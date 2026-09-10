"""The four `validate` count-diff fixes — the newest logic in the safety net.

All four exist because `validate` reported a CORRECT migration as failed, and
all four shipped without a direct test. That combination is worth naming: this
project's whole argument is that a validation layer earns trust by what it
actually checks, and these are the checks doing the checking.

The failure mode is not a crash. Each of these gets a row count subtly wrong,
which either fails a good migration (noise that trains a user to ignore
`validate`) or, in the shared-target case, compares against numbers that were
never meaningful.
"""

from __future__ import annotations

from decimal import Decimal

import mongomock
import pytest

from mongopg_migrate.report.validate import (
    _idmap_row_count,
    _match_stored_scale,
    _mongo_value_occurrences,
)

# ── _match_stored_scale ──────────────────────────────────────────────────────
# A Mongo float carries binary noise a numeric(p,s) column rounds away on the
# way in. The column is right and the comparison was wrong; every such row was
# reported as a value mismatch, burying the real ones.


def test_a_float_is_rounded_to_the_scale_the_column_kept():
    assert _match_stored_scale(91967.50000000001, Decimal("91967.50")) == Decimal("91967.50")


def test_a_genuine_difference_still_survives_rounding():
    # The point is to remove noise, not to make mismatches disappear.
    assert _match_stored_scale(91968.00, Decimal("91967.50")) != Decimal("91967.50")


def test_rounding_respects_the_stored_scale_rather_than_a_fixed_one():
    assert _match_stored_scale(1.23456, Decimal("1.2346")) == Decimal("1.2346")
    assert _match_stored_scale(1.23456, Decimal("1.2")) == Decimal("1.2")


def test_an_integer_valued_decimal_is_left_alone():
    # exponent >= 0 means the column kept no decimal places, so quantizing to
    # it would mask a real 1 vs 1.4 difference.
    assert _match_stored_scale(1.4, Decimal("1E+1")) == 1.4


@pytest.mark.parametrize("actual", [1, 1.5, "1.50", None])
def test_a_non_decimal_stored_value_is_not_rounded(actual):
    assert _match_stored_scale(1.23456, actual) == 1.23456


@pytest.mark.parametrize("recomputed", ["text", None, [1], {"a": 1}])
def test_a_non_numeric_recomputed_value_passes_through(recomputed):
    assert _match_stored_scale(recomputed, Decimal("1.00")) == recomputed


def test_a_bool_is_not_treated_as_a_number():
    """bool is a subclass of int, so it reaches the numeric branch.

    Pinned as BEHAVIOUR, not as proof of the explicit `isinstance(recomputed,
    bool)` guard: removing that guard does not fail this test, because
    `Decimal(str(True))` raises InvalidOperation and the existing except
    returns the value unchanged anyway. Two mechanisms, one outcome. The
    guard is worth keeping — it states the intent, and does not depend on an
    exception being raised by a stringified bool — but it is not load-bearing,
    and this test should not be read as showing that it is.
    """
    assert _match_stored_scale(True, Decimal("1.00")) is True
    assert _match_stored_scale(False, Decimal("1.00")) is False


def test_an_unquantizable_value_is_returned_unchanged_rather_than_raising():
    # A value needing more integer digits than the target allows raises
    # InvalidOperation; validate must report a mismatch, not crash.
    assert _match_stored_scale(float("inf"), Decimal("1.00")) == float("inf")


# ── _mongo_value_occurrences ─────────────────────────────────────────────────
# skip_row drops ROWS, so the expected reduction is the number of rows
# carrying a dangling value — not the number of distinct dangling values.


@pytest.fixture
def db():
    return mongomock.MongoClient().db


def test_counts_rows_per_value_not_distinct_values(db):
    # The shape of the real bug: three rows, one shared reference. Counting
    # distinct values expects 1; the loader actually drops 3.
    db.docs.insert_many([{"ref": "A"}, {"ref": "A"}, {"ref": "A"}, {"ref": "B"}])
    assert _mongo_value_occurrences(db, "docs", None, "ref", {}) == {"A": 3, "B": 1}


def test_the_sum_is_what_gets_subtracted(db):
    # Distinct dicts on purpose: a repeated literal is one object, and the
    # driver stamps an _id onto it after the first insert.
    db.docs.insert_many([{"ref": "gone", "n": i} for i in range(660)] + [{"ref": "kept"}])
    occurrences = _mongo_value_occurrences(db, "docs", None, "ref", {})
    # What `_skip_row_reduction` subtracts is this sum — 660 rows — not the
    # single distinct value "gone" that the pre-fix code counted.
    assert sum(n for v, n in occurrences.items() if v == "gone") == 660
    assert len([v for v in occurrences if v == "gone"]) == 1


def test_missing_and_null_values_are_not_counted(db):
    # An absent reference is not a dangling one; it never reaches the lookup.
    db.docs.insert_many([{"ref": "A"}, {"ref": None}, {"other": 1}])
    assert _mongo_value_occurrences(db, "docs", None, "ref", {}) == {"A": 1}


def test_values_are_keyed_as_strings(db):
    # id_map source_ids are strings everywhere else; the comparison against
    # `known` would never match otherwise.
    db.docs.insert_many([{"ref": 7}, {"ref": 7}])
    assert _mongo_value_occurrences(db, "docs", None, "ref", {}) == {"7": 2}


def test_a_discriminator_filter_scopes_the_count(db):
    # Without the filter a polymorphic collection counts the other variant's
    # rows too, and over-subtracts.
    db.docs.insert_many([{"kind": "a", "ref": "X"}, {"kind": "b", "ref": "X"}])
    assert _mongo_value_occurrences(db, "docs", None, "ref", {"kind": "a"}) == {"X": 1}


def test_an_explode_path_counts_array_items_not_documents(db):
    # skip_row inside an explode drops one ARRAY ITEM, so the unit has to be
    # items. Two documents, three items carrying the value.
    db.docs.insert_many(
        [
            {"items": [{"ref": "A"}, {"ref": "A"}]},
            {"items": [{"ref": "A"}, {"ref": "B"}]},
        ]
    )
    assert _mongo_value_occurrences(db, "docs", "items", "ref", {}) == {"A": 3, "B": 1}


def test_an_empty_collection_yields_no_expectation(db):
    assert _mongo_value_occurrences(db, "docs", None, "ref", {}) == {}


# ── _idmap_row_count ─────────────────────────────────────────────────────────
# When several entities write ONE target table, count(*) on that table is not
# any single entity's row count, so the comparison is meaningless:
#     [MISMATCH] sessions_user     (sessions): mongo=890 postgres=1365
#     [MISMATCH] sessions_hospital (sessions): mongo=476 postgres=1365
# 889 + 476 = 1365 was exactly right.


class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((str(sql), params))

    def fetchone(self):
        return (self.conn.result,)


class _FakeConn:
    def __init__(self, result=0):
        self.result = result
        self.executed: list[tuple] = []

    def cursor(self):
        return _FakeCursor(self)


def test_counts_only_the_named_entitys_id_map_rows():
    conn = _FakeConn(result=890)
    assert _idmap_row_count(conn, "sessions_user", schema="_mongopg") == 890
    sql, params = conn.executed[0]
    assert params == ("sessions_user",), "the entity name must scope the count"
    assert "id_map" in sql


def test_the_internal_schema_is_honoured():
    # A dry run points at a disposable schema; counting the real one would
    # compare against a different migration's rows entirely.
    conn = _FakeConn()
    _idmap_row_count(conn, "orders", schema="_mongopg_dryrun_abc")
    assert '"_mongopg_dryrun_abc"' in conn.executed[0][0]


# ── _entity_skipped_doc_ids ──────────────────────────────────────────────────
# A document dropped by an entity-level skip_row takes its explode / junction
# / unpivot children with it — load.py raises before any child row is
# appended — while the count diff only ever subtracted the parent. One skipped
# document was enough to fail an otherwise perfect migration:
#     [MISMATCH] hospital_details.facilities: mongo=14830 postgres=14829


class _IdMapCursor(_FakeCursor):
    def fetchall(self):
        return [(v,) for v in self.conn.known]


class _IdMapConn(_FakeConn):
    def __init__(self, known=()):
        super().__init__()
        self.known = list(known)

    def cursor(self):
        return _IdMapCursor(self)


def _entity_with_skip_row(source="docs", key="ref", lookup="parents"):
    from mongopg_migrate.mapping.schema import (
        EntityMapping,
        FieldSpec,
        IdStrategy,
        IdStrategyType,
        OnMissing,
    )

    return EntityMapping(
        source=source,
        target=source,
        id_strategy=IdStrategy(type=IdStrategyType.OBJECTID_TO_UUID, source_field="_id"),
        fields={key: FieldSpec(target="parent_id", lookup=lookup, on_missing=OnMissing.SKIP_ROW)},
    )


def _skipped(db, conn, entity, mongo_filter=None):
    from mongopg_migrate.report.validate import _entity_skipped_doc_ids

    return _entity_skipped_doc_ids(
        db, conn, entity, mongo_filter=mongo_filter or {},
        internal_schema="_mongopg", external_conns=None,
    )


def test_identifies_documents_whose_reference_does_not_resolve(db):
    db.docs.insert_many([{"_id": 1, "ref": "known"}, {"_id": 2, "ref": "dangling"}])
    assert _skipped(db, _IdMapConn(known=["known"]), _entity_with_skip_row()) == {2}


def test_a_document_with_no_reference_at_all_is_not_counted_here(db):
    # An ABSENT value is not a dangling one. It is handled separately, by the
    # id-field branch of _skip_row_reduction; counting it here would
    # double-subtract its children.
    db.docs.insert_many([{"_id": 1, "ref": None}, {"_id": 2}])
    assert _skipped(db, _IdMapConn(known=["known"]), _entity_with_skip_row()) == set()


def test_nothing_is_skipped_when_every_reference_resolves(db):
    db.docs.insert_many([{"_id": 1, "ref": "a"}, {"_id": 2, "ref": "b"}])
    assert _skipped(db, _IdMapConn(known=["a", "b"]), _entity_with_skip_row()) == set()


def test_a_field_without_skip_row_contributes_nothing(db):
    from mongopg_migrate.mapping.schema import (
        EntityMapping,
        FieldSpec,
        IdStrategy,
        IdStrategyType,
        OnMissing,
    )

    entity = EntityMapping(
        source="docs", target="docs",
        id_strategy=IdStrategy(type=IdStrategyType.OBJECTID_TO_UUID, source_field="_id"),
        fields={"ref": FieldSpec(target="parent_id", lookup="parents", on_missing=OnMissing.NULL)},
    )
    db.docs.insert_one({"_id": 1, "ref": "dangling"})
    # `null` keeps the row (and its children), so nothing is subtracted.
    assert _skipped(db, _IdMapConn(known=[]), entity) == set()


def test_a_discriminator_filter_scopes_which_documents_are_examined(db):
    db.docs.insert_many(
        [{"_id": 1, "kind": "a", "ref": "x"}, {"_id": 2, "kind": "b", "ref": "x"}]
    )
    got = _skipped(db, _IdMapConn(known=[]), _entity_with_skip_row(), mongo_filter={"kind": "a"})
    assert got == {1}
