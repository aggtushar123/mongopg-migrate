"""`--internal-schema` and `--uuid-namespace`, against real databases.

Both were parameterised in code and unreachable from outside it, which made
them not really options at all:

  --internal-schema  an organisation whose DBA will not grant a schema named
                     `_mongopg` had no way to run the tool.
  --uuid-namespace   an organisation that already minted ObjectId-derived
                     UUIDs in an earlier cutover could not reproduce them, so
                     every foreign key to previously-migrated data was wrong,
                     with no workaround short of editing installed source.

The namespace flag is also the more dangerous of the two, because changing it
partway through a migration silently duplicates instead of resuming. That
guard is the last test here and is the reason the flag is safe to expose.
"""

from __future__ import annotations

import uuid

import pytest
from bson import ObjectId
from click.testing import CliRunner

from mongopg_migrate.cli import main
from mongopg_migrate.migrate.idstrategy import NAMESPACE as DEFAULT_NS

D1 = ObjectId("64f6e1000000000000000001")
D2 = ObjectId("64f6e1000000000000000002")
CUSTOM_NS = uuid.UUID("11111111-2222-4333-8444-555555555555")

MAPPING = """
entities:
  items:
    source: items_hatch_test
    target: items_hatch_test
    id_strategy: {type: objectid_to_uuid, source_field: _id}
    fields:
      label: label
"""


@pytest.fixture
def mapping_file(tmp_path):
    p = tmp_path / "mapping.yaml"
    p.write_text(MAPPING)
    return str(p)


@pytest.fixture
def seeded(mongo_db, pg_conn):
    mongo_db.items_hatch_test.delete_many({})
    mongo_db.items_hatch_test.insert_many([{"_id": D1, "label": "one"}, {"_id": D2, "label": "two"}])
    with pg_conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS items_hatch_test CASCADE")
        cur.execute('DROP SCHEMA IF EXISTS "_mongopg" CASCADE')
        cur.execute('DROP SCHEMA IF EXISTS "mpg_custom_internal" CASCADE')
        cur.execute("CREATE TABLE items_hatch_test (id UUID PRIMARY KEY, label TEXT NOT NULL)")
    yield
    with pg_conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS items_hatch_test CASCADE")
        cur.execute('DROP SCHEMA IF EXISTS "_mongopg" CASCADE')
        cur.execute('DROP SCHEMA IF EXISTS "mpg_custom_internal" CASCADE')
    mongo_db.items_hatch_test.delete_many({})


def _migrate(mapping_file, mongo_uri, postgres_uri, *extra):
    return CliRunner().invoke(
        main,
        ["migrate", mapping_file, "--mongo-uri", mongo_uri, "--postgres-uri", postgres_uri,
         "--mode", "truncate", *extra],
    )


def _schema_exists(pg_conn, name):
    with pg_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", (name,))
        return cur.fetchone() is not None


def _ids(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("SELECT id FROM items_hatch_test ORDER BY label")
        return [str(r[0]) for r in cur.fetchall()]


# ── --internal-schema ────────────────────────────────────────────────────────


def test_internal_state_goes_where_the_flag_says(seeded, mapping_file, pg_conn, mongo_uri, postgres_uri):
    result = _migrate(mapping_file, mongo_uri, postgres_uri, "--internal-schema", "mpg_custom_internal")
    assert result.exit_code == 0, result.output
    assert _schema_exists(pg_conn, "mpg_custom_internal")
    assert not _schema_exists(pg_conn, "_mongopg"), "the default schema was created anyway"

    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM mpg_custom_internal.id_map")
        assert cur.fetchone()[0] == 2
        cur.execute("SELECT count(*) FROM mpg_custom_internal.load_checkpoint")
        assert cur.fetchone()[0] == 1


def test_validate_finds_state_in_the_same_custom_schema(seeded, mapping_file, mongo_uri, postgres_uri):
    assert _migrate(mapping_file, mongo_uri, postgres_uri,
                    "--internal-schema", "mpg_custom_internal").exit_code == 0
    result = CliRunner().invoke(
        main,
        ["validate", mapping_file, "--mongo-uri", mongo_uri, "--postgres-uri", postgres_uri,
         "--internal-schema", "mpg_custom_internal"],
    )
    assert result.exit_code == 0, result.output
    assert "mongo=2 postgres=2" in result.output


def test_validate_against_the_wrong_internal_schema_says_so(seeded, mapping_file, mongo_uri, postgres_uri):
    # The mistake this flag makes possible: migrate somewhere, validate
    # elsewhere. It must be an error, not a silent clean pass.
    assert _migrate(mapping_file, mongo_uri, postgres_uri,
                    "--internal-schema", "mpg_custom_internal").exit_code == 0
    result = CliRunner().invoke(
        main,
        ["validate", mapping_file, "--mongo-uri", mongo_uri, "--postgres-uri", postgres_uri],
    )
    assert result.exit_code != 0
    assert "has `migrate` been run" in result.output


def test_a_dry_run_honours_it_and_leaves_nothing_behind(seeded, mapping_file, pg_conn, mongo_uri, postgres_uri):
    result = CliRunner().invoke(
        main,
        ["dry-run", mapping_file, "--mongo-uri", mongo_uri, "--postgres-uri", postgres_uri,
         "--internal-schema", "mpg_custom_internal", "--realistic"],
    )
    assert result.exit_code == 0, result.output
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM information_schema.schemata WHERE schema_name LIKE %s",
                    ("_mongopg_dryrun_%",))
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM items_hatch_test")
        assert cur.fetchone()[0] == 0


# ── --uuid-namespace ─────────────────────────────────────────────────────────


def test_the_default_namespace_is_unchanged(seeded, mapping_file, pg_conn, mongo_uri, postgres_uri):
    # The flag must not alter existing behaviour when it is not passed.
    assert _migrate(mapping_file, mongo_uri, postgres_uri).exit_code == 0
    assert _ids(pg_conn) == [str(uuid.uuid5(DEFAULT_NS, str(D1))), str(uuid.uuid5(DEFAULT_NS, str(D2)))]


def test_a_custom_namespace_produces_the_ids_that_namespace_derives(
    seeded, mapping_file, pg_conn, mongo_uri, postgres_uri
):
    # The whole point: reproduce ids an earlier cutover minted elsewhere.
    assert _migrate(mapping_file, mongo_uri, postgres_uri,
                    "--uuid-namespace", str(CUSTOM_NS)).exit_code == 0
    assert _ids(pg_conn) == [str(uuid.uuid5(CUSTOM_NS, str(D1))), str(uuid.uuid5(CUSTOM_NS, str(D2)))]


def test_a_custom_namespace_gives_different_ids_than_the_default(
    seeded, mapping_file, pg_conn, mongo_uri, postgres_uri
):
    assert _migrate(mapping_file, mongo_uri, postgres_uri,
                    "--uuid-namespace", str(CUSTOM_NS)).exit_code == 0
    assert _ids(pg_conn) != [str(uuid.uuid5(DEFAULT_NS, str(D1))), str(uuid.uuid5(DEFAULT_NS, str(D2)))]


def test_a_malformed_namespace_is_rejected_by_the_parser(seeded, mapping_file, mongo_uri, postgres_uri):
    result = _migrate(mapping_file, mongo_uri, postgres_uri, "--uuid-namespace", "not-a-uuid")
    assert result.exit_code != 0
    assert "not-a-uuid" in result.output


def test_resuming_under_a_different_namespace_is_refused(
    seeded, mapping_file, pg_conn, mongo_db, mongo_uri, postgres_uri
):
    """The guard that makes this flag safe to expose.

    Every document would resolve to a new UUID, so a resume would insert a
    second copy of every row rather than continue — and nothing else would
    complain. The load would succeed and only a later count diff would show
    the duplication.
    """
    assert _migrate(mapping_file, mongo_uri, postgres_uri).exit_code == 0
    mongo_db.items_hatch_test.insert_one({"_id": ObjectId("64f6e1000000000000000003"), "label": "three"})

    result = CliRunner().invoke(
        main,
        ["migrate", mapping_file, "--mongo-uri", mongo_uri, "--postgres-uri", postgres_uri,
         "--mode", "append", "--uuid-namespace", str(CUSTOM_NS)],
    )
    assert result.exit_code != 0, "a namespace change was allowed to resume"
    assert "different uuid namespace" in result.output
    assert _ids(pg_conn) == [str(uuid.uuid5(DEFAULT_NS, str(D1))), str(uuid.uuid5(DEFAULT_NS, str(D2)))], (
        "the refused run still wrote rows"
    )


def test_resuming_with_the_same_namespace_is_allowed(
    seeded, mapping_file, pg_conn, mongo_db, mongo_uri, postgres_uri
):
    # The guard must not block the ordinary case it sits in front of.
    assert _migrate(mapping_file, mongo_uri, postgres_uri,
                    "--uuid-namespace", str(CUSTOM_NS)).exit_code == 0
    mongo_db.items_hatch_test.insert_one({"_id": ObjectId("64f6e1000000000000000003"), "label": "three"})

    result = CliRunner().invoke(
        main,
        ["migrate", mapping_file, "--mongo-uri", mongo_uri, "--postgres-uri", postgres_uri,
         "--mode", "append", "--uuid-namespace", str(CUSTOM_NS)],
    )
    assert result.exit_code == 0, result.output
    assert len(_ids(pg_conn)) == 3
