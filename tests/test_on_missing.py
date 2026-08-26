"""Coverage for `on_missing` — the policy for a *dangling* `lookup:`
reference (the source field points at a real value, but nothing in
`_mongopg.id_map` resolves it, e.g. the referenced document was deleted).
Distinct from the field being absent/null in the first place, which was
already fine before this existed. Previously the tool had no policy here
at all: any dangling reference was an unconditional `LoadError`, hard-
stopping the whole batch with no way to say "I know about this."

Traced against a real mapping (a `KycVerificationStep.mcmUserId` pointing
at a deleted `McmUser`) by another session before any of this was built —
`error` (unchanged default), `null` (write NULL, still subject to the
target column's NOT NULL check), and `skip_row` (drop just the row this
field's value belongs to — the whole document for a top-level field, one
array item for an explode field, one junction row for a junction — never
more than that) are all covered here across schema validation, the
load.py write path, dryrun.py's Layer A, and report/validate.py.
"""

import mongomock
import pytest

from mongopg_migrate.introspect.postgres import ColumnInfo, PostgresSchema, TableSchema
from mongopg_migrate.mapping.schema import (
    EntityMapping,
    ExplodeSpec,
    FieldSpec,
    ForeignKeyRef,
    IdStrategy,
    IdStrategyType,
    JunctionSpec,
    MappingFile,
    OnMissing,
)
from mongopg_migrate.migrate.dryrun import _validate_field, run_fast_pass
from mongopg_migrate.migrate.load import (
    LoadError,
    OnMissingStats,
    SkipRowError,
    _collect_explode_rows,
    _resolve_field_value,
    _resolve_lookup,
)
from mongopg_migrate.report.validate import (
    _LOOKUP_MISSING,
    _mongo_present_array_values,
    _mongo_present_scalar_values,
    _recompute_field_value,
)


def col(name: str, data_type: str, nullable: bool = True) -> ColumnInfo:
    return ColumnInfo(name=name, data_type=data_type, is_nullable=nullable, default=None)


# --- schema.py: validation ------------------------------------------------------------------


def test_on_missing_null_requires_lookup():
    with pytest.raises(ValueError, match="only applies to a `lookup:` field"):
        FieldSpec(target="status", on_missing="null")


def test_on_missing_skip_row_requires_lookup():
    with pytest.raises(ValueError, match="only applies to a `lookup:` field"):
        FieldSpec(target="status", on_missing="skip_row")


def test_on_missing_default_is_error_and_needs_no_lookup():
    f = FieldSpec(target="status")
    assert f.on_missing == OnMissing.ERROR


def test_on_missing_with_lookup_is_accepted():
    f = FieldSpec(target="user_id", lookup="users", on_missing="null")
    assert f.on_missing == OnMissing.NULL


def test_on_missing_bare_yaml_null_is_coerced_to_the_null_policy():
    # `on_missing: null` unquoted in YAML parses to Python None, not the
    # string "null" — found live, writing exactly this mapping. Without
    # the coercion this raises a confusing enum-validation error for the
    # single most natural way to write the policy name.
    import yaml

    from mongopg_migrate.mapping.schema import MappingFile

    parsed = yaml.safe_load(
        """
        entities:
          bookings:
            source: bookingPayment
            target: bookings
            id_strategy: {type: objectid_to_uuid, source_field: _id}
            fields:
              mcmUserId: {target: user_id, lookup: users, on_missing: null}
        """
    )
    mapping = MappingFile(**parsed)
    assert mapping.entities["bookings"].fields["mcmUserId"].on_missing == OnMissing.NULL


def test_junction_on_missing_rejects_null():
    with pytest.raises(Exception, match="on_missing"):
        JunctionSpec(
            target="order_tags",
            parent_fk=ForeignKeyRef(target_field="order_id", references="orders.id"),
            child_fk=ForeignKeyRef(target_field="tag_id", references="tags.id", lookup="tags"),
            on_missing="null",
        )


def test_junction_on_missing_skip_row_requires_child_lookup():
    with pytest.raises(ValueError, match="child_fk.lookup"):
        JunctionSpec(
            target="order_tags",
            parent_fk=ForeignKeyRef(target_field="order_id", references="orders.id"),
            child_fk=ForeignKeyRef(target_field="tag_id", references="tags.id"),  # no lookup
            on_missing="skip_row",
        )


def test_junction_on_missing_skip_row_accepted_with_lookup():
    j = JunctionSpec(
        target="order_tags",
        parent_fk=ForeignKeyRef(target_field="order_id", references="orders.id"),
        child_fk=ForeignKeyRef(target_field="tag_id", references="tags.id", lookup="tags"),
        on_missing="skip_row",
    )
    assert j.on_missing == OnMissing.SKIP_ROW


# --- load.py: _resolve_lookup -------------------------------------------------------------


class _FakeCursor:
    def __init__(self, fetchone_result=None):
        self._fetchone_result = fetchone_result

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        pass

    def fetchone(self):
        return self._fetchone_result


class _FakeMissConn:
    """Simulates idmap.get() finding no row — a dangling reference."""

    def cursor(self):
        return _FakeCursor(fetchone_result=None)


def _pg_schema_with_user_id(nullable: bool = True) -> PostgresSchema:
    return PostgresSchema(
        tables={
            "bookings": TableSchema(
                name="bookings",
                columns={"id": col("id", "uuid"), "user_id": col("user_id", "uuid", nullable=nullable)},
                primary_key=["id"],
            )
        }
    )


def test_resolve_lookup_default_error_raises_on_miss():
    with pytest.raises(LoadError, match="lookup miss"):
        _resolve_lookup(
            _FakeMissConn(), "users", "deleted-user-id", "bookings", "user_id", _pg_schema_with_user_id(),
        )


def test_resolve_lookup_null_returns_none_and_records_stat():
    stats = OnMissingStats()
    value = _resolve_lookup(
        _FakeMissConn(), "users", "deleted-user-id", "bookings", "user_id", _pg_schema_with_user_id(),
        on_missing=OnMissing.NULL, stats=stats, stats_key="bookings.mcmUserId",
    )
    assert value is None
    assert stats.nulled == {"bookings.mcmUserId": 1}
    assert stats.skipped == {}


def test_resolve_lookup_null_counts_multiple_misses():
    stats = OnMissingStats()
    for _ in range(3):
        _resolve_lookup(
            _FakeMissConn(), "users", "deleted-user-id", "bookings", "user_id", _pg_schema_with_user_id(),
            on_missing=OnMissing.NULL, stats=stats, stats_key="bookings.mcmUserId",
        )
    assert stats.nulled == {"bookings.mcmUserId": 3}


def test_resolve_lookup_skip_row_raises_skiprowerror_and_records_stat():
    stats = OnMissingStats()
    with pytest.raises(SkipRowError):
        _resolve_lookup(
            _FakeMissConn(), "users", "deleted-user-id", "bookings", "user_id", _pg_schema_with_user_id(),
            on_missing=OnMissing.SKIP_ROW, stats=stats, stats_key="bookings.mcmUserId",
        )
    assert stats.skipped == {"bookings.mcmUserId": 1}
    assert stats.nulled == {}


def test_resolve_lookup_none_source_value_is_not_a_miss():
    # Absent/null source field was always fine — never touches on_missing at all.
    stats = OnMissingStats()
    value = _resolve_lookup(
        _FakeMissConn(), "users", None, "bookings", "user_id", _pg_schema_with_user_id(),
        on_missing=OnMissing.ERROR, stats=stats,
    )
    assert value is None
    assert stats.nulled == {} and stats.skipped == {}


# --- load.py: _resolve_field_value ---------------------------------------------------------


def test_resolve_field_value_null_into_nullable_column_succeeds():
    fspec = FieldSpec(target="user_id", lookup="users", on_missing="null")
    value = _resolve_field_value(
        {"mcmUserId": "deleted-user-id"}, "mcmUserId", fspec, context="bookings",
        conn=_FakeMissConn(), pg_schema=_pg_schema_with_user_id(nullable=True), target_table="bookings",
    )
    assert value is None


def test_resolve_field_value_null_into_not_null_column_still_raises():
    # on_missing=null can rescue the LOOKUP, not a real NOT NULL schema
    # mismatch — that's a deliberate, unchanged guarantee, not a gap.
    fspec = FieldSpec(target="user_id", lookup="users", on_missing="null")
    with pytest.raises(LoadError, match="NOT NULL"):
        _resolve_field_value(
            {"mcmUserId": "deleted-user-id"}, "mcmUserId", fspec, context="bookings",
            conn=_FakeMissConn(), pg_schema=_pg_schema_with_user_id(nullable=False), target_table="bookings",
        )


def test_resolve_field_value_skip_row_propagates_skiprowerror():
    fspec = FieldSpec(target="user_id", lookup="users", on_missing="skip_row")
    with pytest.raises(SkipRowError):
        _resolve_field_value(
            {"mcmUserId": "deleted-user-id"}, "mcmUserId", fspec, context="bookings",
            conn=_FakeMissConn(), pg_schema=_pg_schema_with_user_id(), target_table="bookings",
        )


def test_resolve_field_value_default_error_unchanged():
    fspec = FieldSpec(target="user_id", lookup="users")  # on_missing defaults to error
    with pytest.raises(LoadError, match="lookup miss"):
        _resolve_field_value(
            {"mcmUserId": "deleted-user-id"}, "mcmUserId", fspec, context="bookings",
            conn=_FakeMissConn(), pg_schema=_pg_schema_with_user_id(), target_table="bookings",
        )


# --- load.py: _collect_explode_rows skip_row granularity ------------------------------------


def _pg_schema_for_explode() -> PostgresSchema:
    return PostgresSchema(
        tables={
            "order_items": TableSchema(
                name="order_items",
                columns={"order_id": col("order_id", "uuid"), "product_id": col("product_id", "uuid", nullable=False)},
                primary_key=["id"],
            )
        }
    )


def test_collect_explode_rows_skip_row_drops_only_the_offending_item():
    exp = ExplodeSpec(
        target="order_items",
        id_strategy=IdStrategy(type=IdStrategyType.SERIAL),
        parent_fk=ForeignKeyRef(target_field="order_id", references="orders.id"),
        fields={"productId": FieldSpec(target="product_id", lookup="products", on_missing="skip_row")},
    )
    items = [{"productId": "good-1"}, {"productId": "deleted-product"}, {"productId": "good-2"}]
    explode_rows = {"items": []}
    stats = OnMissingStats()

    resolved_uuid = "11111111-1111-1111-1111-111111111111"

    class _MixedConn:
        # good-1/good-2 (1st/3rd call) resolve, deleted-product (2nd call) doesn't.
        def cursor(self):
            _MixedConn._calls += 1
            return _FakeCursor(fetchone_result=None if _MixedConn._calls == 2 else (resolved_uuid,))

    _MixedConn._calls = 0

    _collect_explode_rows(
        "order-uuid", items, exp, path="items", context="orders", conn=_MixedConn(),
        pg_schema=_pg_schema_for_explode(), id_buffers={}, explode_rows=explode_rows,
        internal_schema="_mongopg", external_conns=None, stats=stats,
    )

    assert len(explode_rows["items"]) == 2  # only the offending item was dropped
    assert stats.skipped == {"orders.items.productId": 1}


# --- dryrun.py: _validate_field ------------------------------------------------------------


def test_validate_field_on_missing_error_is_error_severity():
    fspec = FieldSpec(target="user_id", lookup="users")  # default: error
    violations = _validate_field(
        {"mcmUserId": "deleted-user-id"}, "mcmUserId", fspec, context="bookings",
        target_table="bookings", pg_schema=_pg_schema_with_user_id(), found={},
    )
    assert len(violations) == 1
    assert violations[0].severity == "error"
    assert "would fail at migrate time" in violations[0].message


def test_validate_field_on_missing_null_is_info_severity():
    fspec = FieldSpec(target="user_id", lookup="users", on_missing="null")
    violations = _validate_field(
        {"mcmUserId": "deleted-user-id"}, "mcmUserId", fspec, context="bookings",
        target_table="bookings", pg_schema=_pg_schema_with_user_id(), found={},
    )
    assert len(violations) == 1
    assert violations[0].severity == "info"
    assert "will write NULL" in violations[0].message


def test_validate_field_on_missing_null_into_not_null_column_is_still_error():
    # The rescue applies to the LOOKUP, not a genuine NOT NULL schema
    # mismatch — that combination is still a real, blocking problem.
    fspec = FieldSpec(target="user_id", lookup="users", on_missing="null")
    violations = _validate_field(
        {"mcmUserId": "deleted-user-id"}, "mcmUserId", fspec, context="bookings",
        target_table="bookings", pg_schema=_pg_schema_with_user_id(nullable=False), found={},
    )
    assert any(v.severity == "error" and "NOT NULL" in v.message for v in violations)


def test_validate_field_on_missing_skip_row_is_info_severity_and_skips_not_null_check():
    fspec = FieldSpec(target="user_id", lookup="users", on_missing="skip_row")
    violations = _validate_field(
        {"mcmUserId": "deleted-user-id"}, "mcmUserId", fspec, context="bookings",
        target_table="bookings", pg_schema=_pg_schema_with_user_id(nullable=False), found={},
    )
    # Even against a NOT NULL column: the row is dropped, not written with
    # a null value, so there's nothing to flag beyond the info notice.
    assert len(violations) == 1
    assert violations[0].severity == "info"
    assert "will drop this row" in violations[0].message


def test_validate_field_clean_hit_is_no_violation():
    fspec = FieldSpec(target="user_id", lookup="users", on_missing="null")
    violations = _validate_field(
        {"mcmUserId": "real-user-id"}, "mcmUserId", fspec, context="bookings",
        target_table="bookings", pg_schema=_pg_schema_with_user_id(), found={"users": {"real-user-id"}},
    )
    assert violations == []


# --- dryrun.py: run_fast_pass end-to-end (mongomock) ---------------------------------------


@pytest.fixture
def mongo_client():
    client = mongomock.MongoClient()
    yield client
    client.close()


def _make_kyc_mapping(on_missing: str) -> MappingFile:
    return MappingFile(
        entities={
            "users": EntityMapping(
                source="mcmUsers",
                target="mcm_user",
                id_strategy=IdStrategy(type=IdStrategyType.OBJECTID_TO_UUID, source_field="_id"),
            ),
            "kyc_steps": EntityMapping(
                source="kycVerificationSteps",
                target="kyc_step",
                id_strategy=IdStrategy(type=IdStrategyType.OBJECTID_TO_UUID, source_field="_id"),
                fields={
                    "mcmUserId": FieldSpec(target="user_id", lookup="users", on_missing=on_missing),
                },
            ),
        }
    )


def _kyc_pg_schema(nullable: bool = True) -> PostgresSchema:
    return PostgresSchema(
        tables={
            "mcm_user": TableSchema(name="mcm_user", columns={"id": col("id", "uuid")}, primary_key=["id"]),
            "kyc_step": TableSchema(
                name="kyc_step",
                columns={"id": col("id", "uuid"), "user_id": col("user_id", "uuid", nullable=nullable)},
                primary_key=["id"],
            ),
        }
    )


def test_run_fast_pass_dangling_ref_default_is_a_violation(mongo_client, monkeypatch):
    db = mongo_client["app"]
    db.mcmUsers.insert_one({"_id": mongomock.ObjectId()})
    db.kycVerificationSteps.insert_one(
        {"_id": mongomock.ObjectId(), "mcmUserId": mongomock.ObjectId()}  # never inserted into mcmUsers
    )
    monkeypatch.setattr("mongopg_migrate.migrate.dryrun.MongoClient", lambda uri: mongo_client)
    mongo_client.get_default_database = lambda: db

    report = run_fast_pass(_make_kyc_mapping("error"), "mongodb://fake/app", _kyc_pg_schema())
    assert not report.ok
    assert any("lookup miss" in v.message and v.severity == "error" for v in report.violations)


def test_run_fast_pass_dangling_ref_on_missing_null_is_clean(mongo_client, monkeypatch):
    db = mongo_client["app"]
    db.kycVerificationSteps.insert_one({"_id": mongomock.ObjectId(), "mcmUserId": mongomock.ObjectId()})
    monkeypatch.setattr("mongopg_migrate.migrate.dryrun.MongoClient", lambda uri: mongo_client)
    mongo_client.get_default_database = lambda: db

    report = run_fast_pass(_make_kyc_mapping("null"), "mongodb://fake/app", _kyc_pg_schema())
    assert report.ok  # info-severity only — does not block a clean dry-run
    assert any(v.severity == "info" for v in report.violations)


# --- report/validate.py: _recompute_field_value & presence helpers -------------------------


def test_recompute_field_value_error_policy_returns_lookup_missing_sentinel():
    fspec = FieldSpec(target="user_id", lookup="users")  # default: error
    value = _recompute_field_value(
        {"mcmUserId": "deleted-user-id"}, "mcmUserId", fspec, conn=_FakeMissConn(),
        pg_schema=_pg_schema_with_user_id(), target_table="bookings", internal_schema="_mongopg",
    )
    assert value is _LOOKUP_MISSING


def test_recompute_field_value_null_policy_returns_none_not_sentinel():
    # The fix this exists for: without it, every on_missing=null-rescued
    # row would report a false mismatch at validate time.
    fspec = FieldSpec(target="user_id", lookup="users", on_missing="null")
    value = _recompute_field_value(
        {"mcmUserId": "deleted-user-id"}, "mcmUserId", fspec, conn=_FakeMissConn(),
        pg_schema=_pg_schema_with_user_id(), target_table="bookings", internal_schema="_mongopg",
    )
    assert value is None


def test_mongo_present_scalar_values_top_level(mongo_client):
    db = mongo_client["app"]
    db.kycVerificationSteps.insert_many(
        [
            {"_id": mongomock.ObjectId(), "mcmUserId": "u1"},
            {"_id": mongomock.ObjectId(), "mcmUserId": "u2"},
            {"_id": mongomock.ObjectId()},  # no mcmUserId at all — must not appear
            {"_id": mongomock.ObjectId(), "mcmUserId": None},  # explicit null — must not appear
        ]
    )
    present = _mongo_present_scalar_values(db, "kycVerificationSteps", None, "mcmUserId", {})
    assert present == {"u1", "u2"}


def test_mongo_present_array_values_junction_field(mongo_client):
    db = mongo_client["app"]
    db.orders.insert_many(
        [
            {"_id": mongomock.ObjectId(), "tagIds": ["t1", "t2"]},
            {"_id": mongomock.ObjectId(), "tagIds": ["t2", "t3"]},
        ]
    )
    present = _mongo_present_array_values(db, "orders", "tagIds", {})
    assert present == {"t1", "t2", "t3"}


# --- report/validate.py: CountDiff.matches / _skip_row_reduction ----------------------------
#
# Regression coverage for a real bug found live: without reconciling
# skip_row's deliberate row drop, `validate` reported a clean
# on_missing=skip_row migration as "Validation FAILED" — a genuinely
# intentional, policy-covered reduction looked identical to real data
# loss. mongo=3/postgres=2 with expected_skip=1 must read as OK; without
# reconciliation (expected_skip=0) the same counts must read as a mismatch
# — both sides of that behavior are asserted below so a regression that
# quietly removes the reconciliation would be caught either way.


def test_count_diff_matches_accounts_for_expected_skip():
    from mongopg_migrate.report.validate import CountDiff

    assert CountDiff(entity="kyc_step_test", table="kyc_step_test", mongo_count=3, postgres_count=2, expected_skip=1).matches


def test_count_diff_without_expected_skip_still_flags_a_real_mismatch():
    from mongopg_migrate.report.validate import CountDiff

    assert not CountDiff(entity="kyc_step_test", table="kyc_step_test", mongo_count=3, postgres_count=2).matches


def test_skip_row_reduction_counts_only_skip_row_fields(mongo_client):
    from mongopg_migrate.report.validate import _skip_row_reduction

    db = mongo_client["app"]
    entity = EntityMapping(
        source="kycVerificationSteps",
        target="kyc_step",
        id_strategy=IdStrategy(type=IdStrategyType.OBJECTID_TO_UUID, source_field="_id"),
    )
    db.kycVerificationSteps.insert_many(
        [
            {"_id": mongomock.ObjectId(), "mcmUserId": "u-real"},
            {"_id": mongomock.ObjectId(), "mcmUserId": "u-deleted"},  # dangling
        ]
    )
    fields = {"mcmUserId": FieldSpec(target="user_id", lookup="users", on_missing="skip_row")}

    class _KnownUsersConn:
        # Only "u-real" is a known id_map source_id — "u-deleted" is dangling.
        def cursor(self):
            return _FakeSourceIdCursor(["u-real"])

    reduction = _skip_row_reduction(
        db, _KnownUsersConn(), entity, fields, explode_path=None, mongo_filter={},
        internal_schema="_mongopg", external_conns=None,
    )
    assert reduction == 1


def test_skip_row_reduction_ignores_error_and_null_policies(mongo_client):
    from mongopg_migrate.report.validate import _skip_row_reduction

    db = mongo_client["app"]
    entity = EntityMapping(
        source="kycVerificationSteps",
        target="kyc_step",
        id_strategy=IdStrategy(type=IdStrategyType.OBJECTID_TO_UUID, source_field="_id"),
    )
    db.kycVerificationSteps.insert_one({"_id": mongomock.ObjectId(), "mcmUserId": "u-deleted"})
    fields = {"mcmUserId": FieldSpec(target="user_id", lookup="users", on_missing="null")}

    class _NoneKnownConn:
        def cursor(self):
            return _FakeSourceIdCursor([])

    reduction = _skip_row_reduction(
        db, _NoneKnownConn(), entity, fields, explode_path=None, mongo_filter={},
        internal_schema="_mongopg", external_conns=None,
    )
    assert reduction == 0  # on_missing=null never reduces a row count — the row still exists


class _FakeSourceIdCursor:
    def __init__(self, known_ids):
        self._known_ids = known_ids

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        pass

    def fetchall(self):
        return [(i,) for i in self._known_ids]
