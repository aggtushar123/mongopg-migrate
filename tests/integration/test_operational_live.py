"""Operational safety and control: the truncate prompt, `--only`, progress.

None of these change what a correct migration produces. They change whether a
person can run one safely on something they care about:

  - `--mode truncate` empties every mapped table with no undo, and was one
    shell-history recall away from being aimed at the wrong database.
  - A load printed nothing until an entity finished, so a long run over a slow
    link was indistinguishable from a hung one.
  - There was no way to run part of a mapping, which a phased cutover needs.
"""

from __future__ import annotations

import pytest
from bson import ObjectId
from click.testing import CliRunner

from mongopg_migrate.cli import main

U1 = ObjectId("64f7f1000000000000000001")
P1 = ObjectId("64f7f2000000000000000001")

MAPPING = """
entities:
  people:
    source: people_ops_test
    target: people_ops_test
    id_strategy: {type: objectid_to_uuid, source_field: _id}
    fields:
      name: name
  parts:
    source: parts_ops_test
    target: parts_ops_test
    id_strategy: {type: objectid_to_uuid, source_field: _id}
    fields:
      sku: sku
"""


@pytest.fixture
def mapping_file(tmp_path):
    p = tmp_path / "mapping.yaml"
    p.write_text(MAPPING)
    return str(p)


@pytest.fixture
def seeded(mongo_db, pg_conn):
    for c in ("people_ops_test", "parts_ops_test"):
        mongo_db[c].delete_many({})
    mongo_db.people_ops_test.insert_one({"_id": U1, "name": "Alex"})
    mongo_db.parts_ops_test.insert_one({"_id": P1, "sku": "W-1"})
    with pg_conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS people_ops_test, parts_ops_test CASCADE")
        cur.execute('DROP SCHEMA IF EXISTS "_mongopg" CASCADE')
        cur.execute("CREATE TABLE people_ops_test (id UUID PRIMARY KEY, name TEXT NOT NULL)")
        cur.execute("CREATE TABLE parts_ops_test (id UUID PRIMARY KEY, sku TEXT NOT NULL)")
    yield
    with pg_conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS people_ops_test, parts_ops_test CASCADE")
        cur.execute('DROP SCHEMA IF EXISTS "_mongopg" CASCADE')
    for c in ("people_ops_test", "parts_ops_test"):
        mongo_db[c].delete_many({})


def _run(mapping_file, mongo_uri, postgres_uri, *extra, stdin=None):
    return CliRunner().invoke(
        main,
        ["migrate", mapping_file, "--mongo-uri", mongo_uri, "--postgres-uri", postgres_uri, *extra],
        input=stdin,
    )


def _count(pg_conn, table):
    with pg_conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table}")
        return cur.fetchone()[0]


# ── the truncate prompt ──────────────────────────────────────────────────────


def test_truncate_asks_before_emptying_anything(seeded, mapping_file, mongo_uri, postgres_uri):
    result = _run(mapping_file, mongo_uri, postgres_uri, "--mode", "truncate", stdin="n\n")
    assert "will EMPTY" in result.output
    assert "people_ops_test" in result.output and "parts_ops_test" in result.output


def test_declining_writes_nothing_and_exits_non_zero(seeded, mapping_file, pg_conn, mongo_uri, postgres_uri):
    result = _run(mapping_file, mongo_uri, postgres_uri, "--mode", "truncate", stdin="n\n")
    assert result.exit_code == 1
    assert "Aborted" in result.output
    assert _count(pg_conn, "people_ops_test") == 0


def test_the_prompt_defaults_to_no_on_a_bare_enter(seeded, mapping_file, pg_conn, mongo_uri, postgres_uri):
    # A held-down return key must not empty a database.
    result = _run(mapping_file, mongo_uri, postgres_uri, "--mode", "truncate", stdin="\n")
    assert result.exit_code == 1
    assert _count(pg_conn, "people_ops_test") == 0


def test_the_prompt_does_not_echo_the_password(seeded, tmp_path, mapping_file, mongo_uri, postgres_uri):
    result = _run(mapping_file, mongo_uri, postgres_uri, "--mode", "truncate", stdin="n\n")
    assert "postgres:postgres@" not in result.output
    assert ":***@" in result.output


def test_yes_skips_the_prompt_for_automation(seeded, mapping_file, pg_conn, mongo_uri, postgres_uri):
    result = _run(mapping_file, mongo_uri, postgres_uri, "--mode", "truncate", "--yes")
    assert result.exit_code == 0, result.output
    assert _count(pg_conn, "people_ops_test") == 1


@pytest.mark.parametrize("mode", ["append", "upsert"])
def test_non_destructive_modes_are_not_gated(seeded, mapping_file, mongo_uri, postgres_uri, mode):
    # Only `truncate` deletes; prompting on the others would train people to
    # hit `y` without reading.
    result = _run(mapping_file, mongo_uri, postgres_uri, "--mode", mode)
    assert result.exit_code == 0, result.output
    assert "will EMPTY" not in result.output


# ── --only ───────────────────────────────────────────────────────────────────


def test_only_runs_the_named_entity_alone(seeded, mapping_file, pg_conn, mongo_uri, postgres_uri):
    result = _run(mapping_file, mongo_uri, postgres_uri, "--mode", "append", "--only", "people")
    assert result.exit_code == 0, result.output
    assert _count(pg_conn, "people_ops_test") == 1
    assert _count(pg_conn, "parts_ops_test") == 0


def test_only_is_repeatable(seeded, mapping_file, pg_conn, mongo_uri, postgres_uri):
    result = _run(
        mapping_file, mongo_uri, postgres_uri, "--mode", "append", "--only", "people", "--only", "parts"
    )
    assert result.exit_code == 0, result.output
    assert _count(pg_conn, "people_ops_test") == 1
    assert _count(pg_conn, "parts_ops_test") == 1


def test_truncate_only_empties_the_selected_entities_tables(
    seeded, mapping_file, pg_conn, mongo_uri, postgres_uri
):
    """The dangerous interaction, pinned.

    If `--only` narrowed the load but not the truncate, running one entity of
    a mapping would silently empty the tables of every other entity — data
    this run then never reloads.
    """
    assert _run(mapping_file, mongo_uri, postgres_uri, "--mode", "append").exit_code == 0
    assert _count(pg_conn, "parts_ops_test") == 1

    result = _run(
        mapping_file, mongo_uri, postgres_uri, "--mode", "truncate", "--only", "people", "--yes"
    )
    assert result.exit_code == 0, result.output
    assert _count(pg_conn, "parts_ops_test") == 1, "an unselected entity's table was emptied"


def test_an_unknown_entity_is_rejected_before_anything_runs(
    seeded, mapping_file, pg_conn, mongo_uri, postgres_uri
):
    result = _run(mapping_file, mongo_uri, postgres_uri, "--mode", "append", "--only", "nosuch")
    assert result.exit_code == 1
    assert "not an entity in this mapping file" in result.output
    assert "parts, people" in result.output  # sorted
    # A pre-flight failure must not describe a rollback that never happened.
    assert "rolled back" not in result.output
    assert _count(pg_conn, "people_ops_test") == 0


# ── progress ─────────────────────────────────────────────────────────────────


def test_a_load_reports_progress_while_it_runs(seeded, mapping_file, mongo_uri, postgres_uri):
    result = _run(mapping_file, mongo_uri, postgres_uri, "--mode", "append")
    assert result.exit_code == 0, result.output
    assert "loaded so far" in result.output, "a long run would print nothing until it finished"
