"""Re-running `append`/`upsert` over an entity already marked `done`.

An entity that finished once used to be frozen: documents inserted into the
source afterwards were never picked up, and the only way forward was deleting
the checkpoint row by hand. That was fixed — and then shipped with no test of
any kind. Nothing in `tests/` referenced resuming a `done` entity, so the
behaviour rested entirely on one hand-run session.

It is worth automating for a specific reason: the failure mode is silence. A
frozen entity does not error, it reports success having done nothing, and a
count diff run immediately afterwards reports the mismatch as though the
migration were at fault rather than the resume.
"""

from __future__ import annotations

import pytest
from bson import ObjectId
from click.testing import CliRunner

from mongopg_migrate.cli import main

# _id order is the resume cursor, so these are deliberately ascending.
D1 = ObjectId("64f5c1000000000000000001")
D2 = ObjectId("64f5c1000000000000000002")
D3 = ObjectId("64f5c1000000000000000003")
D4 = ObjectId("64f5c1000000000000000004")

MAPPING = """
entities:
  notes:
    source: notes_resume_test
    target: notes_resume_test
    id_strategy: {type: objectid_to_uuid, source_field: _id}
    fields:
      body: body
"""


@pytest.fixture
def mapping_file(tmp_path):
    p = tmp_path / "mapping.yaml"
    p.write_text(MAPPING)
    return str(p)


@pytest.fixture
def seeded(mongo_db, pg_conn):
    mongo_db.notes_resume_test.delete_many({})
    mongo_db.notes_resume_test.insert_many(
        [{"_id": D1, "body": "one"}, {"_id": D2, "body": "two"}]
    )
    with pg_conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS notes_resume_test CASCADE")
        cur.execute('DROP SCHEMA IF EXISTS "_mongopg" CASCADE')
        cur.execute("CREATE TABLE notes_resume_test (id UUID PRIMARY KEY, body TEXT NOT NULL)")
    yield
    with pg_conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS notes_resume_test CASCADE")
        cur.execute('DROP SCHEMA IF EXISTS "_mongopg" CASCADE')
    mongo_db.notes_resume_test.delete_many({})


def _migrate(mapping_file, mongo_uri, postgres_uri, mode):
    return CliRunner().invoke(
        main,
        ["migrate", mapping_file, "--mongo-uri", mongo_uri, "--postgres-uri", postgres_uri, "--mode", mode],
    )


def _bodies(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("SELECT body FROM notes_resume_test ORDER BY body")
        return [r[0] for r in cur.fetchall()]


def _checkpoint(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("SELECT status, last_source_id FROM _mongopg.load_checkpoint WHERE entity = 'notes'")
        return cur.fetchone()


@pytest.mark.parametrize("mode", ["append", "upsert"])
def test_a_done_entity_picks_up_documents_inserted_since(
    seeded, mapping_file, pg_conn, mongo_db, mongo_uri, postgres_uri, mode
):
    """The regression this exists for: the entity used to freeze after its
    first completion and never look at the source again."""
    assert _migrate(mapping_file, mongo_uri, postgres_uri, mode).exit_code == 0
    assert _bodies(pg_conn) == ["one", "two"]
    assert _checkpoint(pg_conn) == ("done", str(D2))

    mongo_db.notes_resume_test.insert_many([{"_id": D3, "body": "three"}, {"_id": D4, "body": "four"}])

    result = _migrate(mapping_file, mongo_uri, postgres_uri, mode)
    assert result.exit_code == 0, result.output
    assert "resumed after" in result.output
    assert _bodies(pg_conn) == ["four", "one", "three", "two"]


@pytest.mark.parametrize("mode", ["append", "upsert"])
def test_resuming_neither_duplicates_nor_skips(
    seeded, mapping_file, pg_conn, mongo_db, mongo_uri, postgres_uri, mode
):
    # The two ways a resume goes wrong: re-reading a document it already
    # loaded (duplicate, or a PK violation in append mode), or resuming one
    # document too far and losing the boundary row.
    assert _migrate(mapping_file, mongo_uri, postgres_uri, mode).exit_code == 0
    mongo_db.notes_resume_test.insert_one({"_id": D3, "body": "three"})
    assert _migrate(mapping_file, mongo_uri, postgres_uri, mode).exit_code == 0

    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*), count(DISTINCT id) FROM notes_resume_test")
        total, distinct = cur.fetchone()
    assert total == 3, "a document was skipped or duplicated across the resume"
    assert total == distinct, "the resume re-inserted a document it had already loaded"


@pytest.mark.parametrize("mode", ["append", "upsert"])
def test_nothing_new_is_reported_cleanly_and_writes_nothing(
    seeded, mapping_file, pg_conn, mongo_uri, postgres_uri, mode
):
    assert _migrate(mapping_file, mongo_uri, postgres_uri, mode).exit_code == 0
    before = _checkpoint(pg_conn)

    result = _migrate(mapping_file, mongo_uri, postgres_uri, mode)
    assert result.exit_code == 0, result.output
    assert "already fully loaded" in result.output
    assert _bodies(pg_conn) == ["one", "two"], "a no-op run wrote rows"
    assert _checkpoint(pg_conn) == before, "a no-op run moved the checkpoint"


def test_the_checkpoint_advances_to_the_newest_document_after_a_resume(
    seeded, mapping_file, pg_conn, mongo_db, mongo_uri, postgres_uri
):
    # If the checkpoint did not advance, the next run would re-read the
    # documents this one just loaded — in append mode, straight into a
    # primary-key violation.
    assert _migrate(mapping_file, mongo_uri, postgres_uri, "append").exit_code == 0
    mongo_db.notes_resume_test.insert_one({"_id": D3, "body": "three"})
    assert _migrate(mapping_file, mongo_uri, postgres_uri, "append").exit_code == 0
    assert _checkpoint(pg_conn) == ("done", str(D3))

    # A third run must still be a clean no-op rather than a re-read.
    third = _migrate(mapping_file, mongo_uri, postgres_uri, "append")
    assert third.exit_code == 0, third.output
    assert "already fully loaded" in third.output


def test_validate_agrees_after_a_resume(seeded, mapping_file, mongo_db, mongo_uri, postgres_uri):
    # The end that matters to a user: source and target agree once the
    # entity has been resumed, without any manual checkpoint surgery.
    assert _migrate(mapping_file, mongo_uri, postgres_uri, "append").exit_code == 0
    mongo_db.notes_resume_test.insert_many([{"_id": D3, "body": "three"}, {"_id": D4, "body": "four"}])
    assert _migrate(mapping_file, mongo_uri, postgres_uri, "append").exit_code == 0

    validated = CliRunner().invoke(
        main, ["validate", mapping_file, "--mongo-uri", mongo_uri, "--postgres-uri", postgres_uri]
    )
    assert validated.exit_code == 0, validated.output
    assert "mongo=4 postgres=4" in validated.output
