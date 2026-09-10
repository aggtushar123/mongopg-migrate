"""`--mode upsert` against a real Postgres.

The unit tests cover SQL generation and never execute it, so until now nothing
proved that `INSERT ... SELECT ... ON CONFLICT DO UPDATE` through a TEMP
staging table actually behaves the way the generated SQL suggests. Upsert is
the mode you use for a second pass over a live source, which makes "never run
against a real database" a bad place for it to be.

Two behaviours here are surprising enough that they are pinned deliberately
rather than merely exercised — see the tests for a modified document and for
explode children.
"""

from __future__ import annotations

import pytest
from bson import ObjectId
from click.testing import CliRunner

from mongopg_migrate.cli import main

W1 = ObjectId("64f4a1000000000000000001")
W2 = ObjectId("64f4a1000000000000000002")
W3 = ObjectId("64f4a1000000000000000003")
TAG_A = ObjectId("64f4b1000000000000000001")
TAG_B = ObjectId("64f4b1000000000000000002")

MAPPING = """
entities:
  tags:
    source: tags_upsert_test
    target: tags_upsert_test
    id_strategy: {type: objectid_to_uuid, source_field: _id}
    fields:
      label: label
  widgets:
    source: widgets_upsert_test
    target: widgets_upsert_test
    id_strategy: {type: objectid_to_uuid, source_field: _id}
    fields:
      sku: sku
      qty: qty
    explode:
      parts:
        target: widget_parts_upsert_test
        id_strategy: {type: serial}
        parent_fk: {target_field: widget_id, references: widgets_upsert_test.id}
        fields:
          name: name
    junction:
      tagIds:
        target: widget_tags_upsert_test
        parent_fk: {target_field: widget_id, references: widgets_upsert_test.id}
        child_fk: {target_field: tag_id, references: tags_upsert_test.id, lookup: tags}
"""


@pytest.fixture
def mapping_file(tmp_path):
    p = tmp_path / "mapping.yaml"
    p.write_text(MAPPING)
    return str(p)


@pytest.fixture
def seeded(mongo_db, pg_conn):
    for c in ("widgets_upsert_test", "tags_upsert_test"):
        mongo_db[c].delete_many({})
    mongo_db.tags_upsert_test.insert_many(
        [{"_id": TAG_A, "label": "priority"}, {"_id": TAG_B, "label": "gift"}]
    )
    mongo_db.widgets_upsert_test.insert_many(
        [
            {"_id": W1, "sku": "A-1", "qty": 1, "parts": [{"name": "p1"}, {"name": "p2"}], "tagIds": [TAG_A]},
            {"_id": W2, "sku": "B-2", "qty": 2, "parts": [{"name": "p3"}], "tagIds": [TAG_A, TAG_B]},
        ]
    )

    with pg_conn.cursor() as cur:
        cur.execute(
            "DROP TABLE IF EXISTS widget_tags_upsert_test, widget_parts_upsert_test,"
            " widgets_upsert_test, tags_upsert_test CASCADE"
        )
        cur.execute('DROP SCHEMA IF EXISTS "_mongopg" CASCADE')
        cur.execute("""
            CREATE TABLE tags_upsert_test (id UUID PRIMARY KEY, label TEXT NOT NULL);
            CREATE TABLE widgets_upsert_test (id UUID PRIMARY KEY, sku TEXT NOT NULL, qty INT NOT NULL);
            CREATE TABLE widget_parts_upsert_test (
                id SERIAL PRIMARY KEY,
                widget_id UUID NOT NULL REFERENCES widgets_upsert_test(id),
                name TEXT NOT NULL
            );
            CREATE TABLE widget_tags_upsert_test (
                widget_id UUID NOT NULL REFERENCES widgets_upsert_test(id),
                tag_id UUID NOT NULL REFERENCES tags_upsert_test(id),
                PRIMARY KEY (widget_id, tag_id)
            );
        """)
    yield
    with pg_conn.cursor() as cur:
        cur.execute(
            "DROP TABLE IF EXISTS widget_tags_upsert_test, widget_parts_upsert_test,"
            " widgets_upsert_test, tags_upsert_test CASCADE"
        )
        cur.execute('DROP SCHEMA IF EXISTS "_mongopg" CASCADE')
    for c in ("widgets_upsert_test", "tags_upsert_test"):
        mongo_db[c].delete_many({})


def _migrate(mapping_file, mongo_uri, postgres_uri, mode="upsert"):
    return CliRunner().invoke(
        main,
        ["migrate", mapping_file, "--mongo-uri", mongo_uri, "--postgres-uri", postgres_uri, "--mode", mode],
    )


def _clear_checkpoints(pg_conn):
    """What an operator does to force a full re-pass over the source.

    Without this a completed entity only ever looks at documents newer than
    its checkpoint, so the DO UPDATE branch is never reached — see
    test_a_modified_document_is_not_picked_up_while_the_checkpoint_stands.
    """
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM _mongopg.load_checkpoint")


def _counts(pg_conn):
    with pg_conn.cursor() as cur:
        out = {}
        for t in ("widgets_upsert_test", "widget_parts_upsert_test", "widget_tags_upsert_test"):
            cur.execute(f"SELECT count(*) FROM {t}")
            out[t] = cur.fetchone()[0]
        return out


def _qty(pg_conn, sku):
    with pg_conn.cursor() as cur:
        cur.execute("SELECT qty FROM widgets_upsert_test WHERE sku = %s", (sku,))
        return cur.fetchone()[0]


def test_upsert_inserts_on_a_first_run(seeded, mapping_file, pg_conn, mongo_uri, postgres_uri):
    result = _migrate(mapping_file, mongo_uri, postgres_uri)
    assert result.exit_code == 0, result.output
    assert _counts(pg_conn) == {
        "widgets_upsert_test": 2,
        "widget_parts_upsert_test": 3,
        "widget_tags_upsert_test": 3,
    }


def test_upsert_updates_the_main_row_in_place_without_duplicating(
    seeded, mapping_file, pg_conn, mongo_db, mongo_uri, postgres_uri
):
    """The DO UPDATE branch, actually executed.

    This is what `--mode upsert` is for, and it had never run against a real
    Postgres: a second pass over a document that is already loaded must
    rewrite its row rather than fail on the primary key or insert a duplicate.
    """
    assert _migrate(mapping_file, mongo_uri, postgres_uri).exit_code == 0
    mongo_db.widgets_upsert_test.update_one({"_id": W1}, {"$set": {"qty": 99}})
    _clear_checkpoints(pg_conn)

    result = _migrate(mapping_file, mongo_uri, postgres_uri)
    assert result.exit_code == 0, result.output

    assert _qty(pg_conn, "A-1") == 99, "the row was not updated in place"
    assert _counts(pg_conn)["widgets_upsert_test"] == 2, "upsert duplicated a main row"


def test_a_modified_document_is_not_picked_up_while_the_checkpoint_stands(
    seeded, mapping_file, pg_conn, mongo_db, mongo_uri, postgres_uri
):
    """A sharp edge worth knowing before you rely on upsert for a cutover.

    A completed entity resumes at `_id > last_source_id`, and editing a
    document does not change its `_id` — so re-running `--mode upsert` after
    changing existing data does NOT pick the change up, reports "already fully
    loaded", and exits 0. It is not a failed update; the document is never
    looked at.

    Upsert re-writes rows for documents the run actually processes: ones newer
    than the checkpoint, or all of them once the checkpoint is cleared. Pinned
    here because "ran clean, changed nothing" is the shape of problem this
    project keeps finding.
    """
    assert _migrate(mapping_file, mongo_uri, postgres_uri).exit_code == 0
    mongo_db.widgets_upsert_test.update_one({"_id": W1}, {"$set": {"qty": 99}})

    result = _migrate(mapping_file, mongo_uri, postgres_uri)
    assert result.exit_code == 0, result.output
    assert "already fully loaded" in result.output
    assert _qty(pg_conn, "A-1") == 1, "expected the modified document to be skipped entirely"


def test_junction_rows_are_not_duplicated_by_a_second_pass(
    seeded, mapping_file, pg_conn, mongo_uri, postgres_uri
):
    # A junction row is entirely key columns, so upsert compiles to DO NOTHING
    # rather than DO UPDATE. A re-pass must therefore be a no-op, not a
    # primary-key violation.
    assert _migrate(mapping_file, mongo_uri, postgres_uri).exit_code == 0
    _clear_checkpoints(pg_conn)
    result = _migrate(mapping_file, mongo_uri, postgres_uri)
    assert result.exit_code == 0, result.output
    assert _counts(pg_conn)["widget_tags_upsert_test"] == 3


def test_explode_children_are_duplicated_by_a_second_pass_and_validate_catches_it(
    seeded, mapping_file, pg_conn, mongo_uri, postgres_uri
):
    """`explode` children have no natural conflict key, so they are always
    plain-inserted — a documented limitation whose CONSEQUENCE was not.

    Re-processing a document therefore inserts its child rows again, silently,
    with no error from the loader. That is the class of quiet wrongness this
    tool exists to catch, so what actually matters is the second assertion:
    `validate` reports the count mismatch and exits non-zero.

    If a future change gives explode children a conflict key, this test should
    be rewritten to assert no duplication — not deleted.
    """
    assert _migrate(mapping_file, mongo_uri, postgres_uri).exit_code == 0
    assert _counts(pg_conn)["widget_parts_upsert_test"] == 3
    _clear_checkpoints(pg_conn)
    assert _migrate(mapping_file, mongo_uri, postgres_uri).exit_code == 0

    assert _counts(pg_conn)["widget_parts_upsert_test"] == 6, "explode children no longer duplicate"

    validated = CliRunner().invoke(
        main, ["validate", mapping_file, "--mongo-uri", mongo_uri, "--postgres-uri", postgres_uri]
    )
    assert validated.exit_code != 0, "validate passed a table with duplicated child rows"
    assert "mongo=3 postgres=6" in validated.output


def test_a_new_document_upserts_alongside_existing_rows(
    seeded, mapping_file, pg_conn, mongo_db, mongo_uri, postgres_uri
):
    assert _migrate(mapping_file, mongo_uri, postgres_uri).exit_code == 0
    mongo_db.widgets_upsert_test.insert_one(
        {"_id": W3, "sku": "C-3", "qty": 3, "parts": [{"name": "p4"}], "tagIds": [TAG_B]}
    )

    result = _migrate(mapping_file, mongo_uri, postgres_uri)
    assert result.exit_code == 0, result.output
    assert _counts(pg_conn) == {
        "widgets_upsert_test": 3,
        "widget_parts_upsert_test": 4,
        "widget_tags_upsert_test": 4,
    }
