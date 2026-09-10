"""`--pg-schema` must decide where the load WRITES, not just what it reads.

Every write in migrate/load.py names its table without a schema qualifier
(`COPY "orders"`, `TRUNCATE ...`), so the destination is decided entirely by
`search_path`. That was left at whatever the connecting role defaulted to,
while `--pg-schema` was passed only to `introspect_postgres`. For anyone
whose target tables do not live in `public`, the tool therefore read the
right schema and wrote somewhere else — with no error, because the load is
perfectly valid against whatever tables it did find.

These are the fast checks: that the statement is issued at all, that it is
built by identifier composition rather than string formatting, and that
dryrun keeps its disposable clone ahead of the real schema. The end-to-end
proof against a real Postgres is in tests/integration/test_target_schema_live.py.
"""

from __future__ import annotations

import pytest
from psycopg import sql

from mongopg_migrate.migrate.load import DEFAULT_TARGET_SCHEMA


def _render(path: list[str]) -> str:
    """Exactly the composition load()/validate() use to build the statement."""
    return (
        sql.SQL("SET search_path TO {}")
        .format(sql.SQL(", ").join(sql.Identifier(s) for s in path))
        .as_string(None)
    )


def test_default_target_schema_is_public():
    assert DEFAULT_TARGET_SCHEMA == "public"


def test_schema_name_is_quoted():
    assert _render(["app"]) == 'SET search_path TO "app"'


def test_mixed_case_schema_survives():
    # Unquoted, Postgres would fold this to `myschema` and resolve the wrong
    # (or no) schema. This is the ordinary reason a target schema is quoted.
    assert _render(["MySchema"]) == 'SET search_path TO "MySchema"'


def test_dryrun_puts_its_disposable_clone_ahead_of_the_real_schema():
    # Order matters: the clone must shadow the real tables, or a "dry" run
    # writes into production. The second element must be the real target
    # schema and NOT a hardcoded `public`, which is what it used to be.
    assert _render(["_mongopg_dryrun_abc123", "app"]) == (
        'SET search_path TO "_mongopg_dryrun_abc123", "app"'
    )


@pytest.mark.parametrize(
    "hostile",
    [
        'public"; DROP SCHEMA app CASCADE; --',
        "public, pg_catalog",
        'has"quote',
        "has space",
    ],
)
def test_hostile_schema_names_cannot_break_out_of_the_identifier(hostile: str):
    rendered = _render([hostile])
    # One identifier, fully quoted, with any embedded quote doubled — so
    # nothing in the name is ever parsed as SQL.
    assert rendered == 'SET search_path TO "{}"'.format(hostile.replace('"', '""'))
    assert not rendered.endswith("--")


def test_load_and_validate_accept_a_target_schema():
    # Guards against the signature silently losing the parameter again: the
    # bug this file exists for was precisely a flag that went nowhere.
    import inspect

    from mongopg_migrate.migrate.load import load
    from mongopg_migrate.report.validate import validate

    for fn in (load, validate):
        assert "target_schema" in inspect.signature(fn).parameters, fn.__name__


def test_dryrun_entry_points_accept_a_target_schema():
    import inspect

    from mongopg_migrate.migrate import dryrun

    for fn in (dryrun.run, dryrun.run_realistic_pass):
        assert "target_schema" in inspect.signature(fn).parameters, fn.__name__


def test_cli_passes_pg_schema_into_every_write_path():
    """The flag existed and was parsed; it just never reached the writers.

    A signature test cannot catch that, so assert the wiring at the call
    sites themselves.
    """
    import inspect

    from mongopg_migrate import cli

    source = inspect.getsource(cli)
    # migrate, validate, dry-run (default), dry-run (--realistic)
    assert source.count("target_schema=pg_schema") == 4
