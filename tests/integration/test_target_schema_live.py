"""`--pg-schema` decides where the data LANDS — proven against real Postgres.

The bug this pins: every write in migrate/load.py names its table without a
schema qualifier, so the destination is whatever `search_path` resolves.
`--pg-schema` was passed only to `introspect_postgres`, so the tool read the
requested schema and wrote wherever the connecting role happened to point —
normally `public`. Nothing errored, because the load was perfectly valid
against the tables it did find.

The test is built to fail loudly if that regresses: an identically-named
DECOY table is created in `public` alongside the real one in the target
schema. A correct run fills the target and leaves the decoy empty. The
pre-fix code did the exact opposite, which is the assertion that matters —
a test that only checked "the target has rows" would have passed before the
fix too, if `public` were on the search path.
"""

from __future__ import annotations

import datetime

import pytest
from bson import ObjectId
from click.testing import CliRunner

from mongopg_migrate.cli import main

SCHEMA = "mpg_target_schema_test"
COLLECTION = "widgets_schema_test"
TABLE = "widgets_schema_test"

W1 = ObjectId("64f2c1000000000000000001")
W2 = ObjectId("64f2c1000000000000000002")

MAPPING_YAML = f"""
entities:
  widgets:
    source: {COLLECTION}
    target: {TABLE}
    id_strategy: {{type: objectid_to_uuid, source_field: _id}}
    fields:
      sku: sku
      createdAt: {{target: created_at, transform: cast_timestamptz}}
"""

DDL = """
    CREATE TABLE {qualified} (
        id UUID PRIMARY KEY,
        sku TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL
    )
"""


@pytest.fixture
def seeded(mongo_db, pg_conn):
    mongo_db[COLLECTION].delete_many({})
    mongo_db[COLLECTION].insert_many(
        [
            {"_id": W1, "sku": "A-1", "createdAt": datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC)},
            {"_id": W2, "sku": "B-2", "createdAt": datetime.datetime(2026, 3, 2, tzinfo=datetime.UTC)},
        ]
    )

    with pg_conn.cursor() as cur:
        cur.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
        cur.execute(f'DROP TABLE IF EXISTS public."{TABLE}"')
        cur.execute('DROP SCHEMA IF EXISTS "_mongopg" CASCADE')
        cur.execute(f'CREATE SCHEMA "{SCHEMA}"')
        # The real target...
        cur.execute(DDL.format(qualified=f'"{SCHEMA}"."{TABLE}"'))
        # ...and a decoy of the same name in public, which must stay empty.
        cur.execute(DDL.format(qualified=f'public."{TABLE}"'))

    yield

    with pg_conn.cursor() as cur:
        cur.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
        cur.execute(f'DROP TABLE IF EXISTS public."{TABLE}"')
        cur.execute('DROP SCHEMA IF EXISTS "_mongopg" CASCADE')
    mongo_db[COLLECTION].delete_many({})


def _count(pg_conn, qualified: str) -> int:
    with pg_conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {qualified}")
        return cur.fetchone()[0]


def test_migrate_writes_into_pg_schema_not_public(tmp_path, seeded, pg_conn, mongo_uri, postgres_uri):
    mapping = tmp_path / "mapping.yaml"
    mapping.write_text(MAPPING_YAML)

    result = CliRunner().invoke(
        main,
        [
            "migrate", str(mapping),
            "--mongo-uri", mongo_uri,
            "--postgres-uri", postgres_uri,
            "--pg-schema", SCHEMA,
            "--mode", "truncate", "--yes",
        ],
    )
    assert result.exit_code == 0, result.output

    assert _count(pg_conn, f'"{SCHEMA}"."{TABLE}"') == 2, "rows did not land in the requested schema"
    assert _count(pg_conn, f'public."{TABLE}"') == 0, (
        "rows landed in public — --pg-schema was ignored on the write path"
    )


def test_validate_reads_the_same_schema_it_migrated(tmp_path, seeded, pg_conn, mongo_uri, postgres_uri):
    mapping = tmp_path / "mapping.yaml"
    mapping.write_text(MAPPING_YAML)
    runner = CliRunner()

    migrated = runner.invoke(
        main,
        ["migrate", str(mapping), "--mongo-uri", mongo_uri, "--postgres-uri", postgres_uri,
         "--pg-schema", SCHEMA, "--mode", "truncate", "--yes"],
    )
    assert migrated.exit_code == 0, migrated.output

    # Pin WHERE the rows are first. Without this the test passes even when
    # both commands are consistently pointed at the wrong schema: migrate
    # fills the public decoy, validate counts the public decoy, and the
    # totals agree. Agreement is not correctness.
    with pg_conn.cursor() as cur:
        cur.execute(f'SELECT count(*) FROM "{SCHEMA}"."{TABLE}"')
        assert cur.fetchone()[0] == 2
        cur.execute(f'SELECT count(*) FROM public."{TABLE}"')
        assert cur.fetchone()[0] == 0

    # Counted against the empty public decoy this reports 0 vs 2 and fails.
    validated = runner.invoke(
        main,
        ["validate", str(mapping), "--mongo-uri", mongo_uri, "--postgres-uri", postgres_uri,
         "--pg-schema", SCHEMA],
    )
    assert validated.exit_code == 0, validated.output
    assert "mongo=2 postgres=2" in validated.output


def test_dry_run_clone_does_not_write_into_the_real_schema(tmp_path, seeded, pg_conn, mongo_uri, postgres_uri):
    mapping = tmp_path / "mapping.yaml"
    mapping.write_text(MAPPING_YAML)

    # Layer B clones the TARGET schema's tables. Drop the public decoy so a
    # clone of `public` has nothing to copy and cannot quietly succeed —
    # otherwise this test passes whichever schema the clone actually read.
    with pg_conn.cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS public."{TABLE}"')

    result = CliRunner().invoke(
        main,
        ["dry-run", str(mapping), "--mongo-uri", mongo_uri, "--postgres-uri", postgres_uri,
         "--pg-schema", SCHEMA, "--realistic"],
    )
    assert result.exit_code == 0, result.output

    # A dry run clones the target schema and writes only into the clone.
    assert _count(pg_conn, f'"{SCHEMA}"."{TABLE}"') == 0, "dry-run wrote into the real target schema"

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM information_schema.schemata WHERE schema_name LIKE %s",
            ("_mongopg_dryrun_%",),
        )
        assert cur.fetchone()[0] == 0, "dry-run left its disposable schema behind"


def test_unknown_pg_schema_is_reported_not_silently_empty(tmp_path, seeded, mongo_uri, postgres_uri):
    mapping = tmp_path / "mapping.yaml"
    mapping.write_text(MAPPING_YAML)

    result = CliRunner().invoke(
        main,
        ["migrate", str(mapping), "--mongo-uri", mongo_uri, "--postgres-uri", postgres_uri,
         "--pg-schema", "no_such_schema_here", "--mode", "truncate", "--yes"],
    )
    assert result.exit_code != 0
    assert "no_such_schema_here" in result.output
    assert "does not exist" in result.output
