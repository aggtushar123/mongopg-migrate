"""The tool-owned ID lookup table: `_mongopg.id_map`.

PRD §7 (P0): "Tool-owned ID lookup storage: `_mongopg.id_map(entity,
source_id, target_id)` in the target Postgres database, written in the same
transaction/checkpoint as the table load it belongs to. This is the durable
source of truth for cross-entity `lookup:` resolution and for resume; a
file export of the same data is optional and secondary."

Every `lookup: <entity>` reference in a mapping file (PRD §12) resolves
against this table: `SELECT target_id FROM _mongopg.id_map WHERE entity =
%(entity)s AND source_id = %(source_id)s`. Entities whose `id_strategy.type`
is `serial` (PRD §12 — child tables from `explode` with no source id to
preserve) never get rows here, since nothing can `lookup:` a synthetic
per-array-item id.

The schema name is parameterized (default `_mongopg`, `DEFAULT_SCHEMA_NAME`
below) so migrate/dryrun.py's realistic pass can point every call here at a
disposable `_mongopg_dryrun_<...>` schema instead — a dry run must never
write into the real id_map, or a subsequent real `migrate` would see
checkpoint rows that don't correspond to anything actually loaded. Schema
names reaching this module are always tool-generated (the default constant,
or a uuid4-suffixed name — see dryrun.py), never raw user input, since
Postgres has no parameterized-identifier support to escape them with.
"""

from __future__ import annotations

import psycopg

DEFAULT_SCHEMA_NAME = "_mongopg"
TABLE_NAME = "id_map"


def _qualified(schema: str) -> str:
    return f'"{schema}"."{TABLE_NAME}"'


def ddl(schema: str = DEFAULT_SCHEMA_NAME) -> str:
    qualified = _qualified(schema)
    return f"""
CREATE SCHEMA IF NOT EXISTS "{schema}";

CREATE TABLE IF NOT EXISTS {qualified} (
    entity      TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    target_id   TEXT NOT NULL,
    loaded_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (entity, source_id)
);

CREATE INDEX IF NOT EXISTS id_map_entity_target_idx
    ON {qualified} (entity, target_id);
"""


def ensure_schema(conn: psycopg.Connection, *, schema: str = DEFAULT_SCHEMA_NAME) -> None:
    """Create `<schema>.id_map` if it doesn't exist. Idempotent — safe to
    call at the start of every dry-run and every real load."""
    with conn.cursor() as cur:
        cur.execute(ddl(schema))


def put(
    conn: psycopg.Connection, entity: str, source_id: str, target_id: str, *, schema: str = DEFAULT_SCHEMA_NAME
) -> None:
    """Record one ID remapping. Callers are responsible for committing this
    in the same transaction/checkpoint as the row it belongs to (PRD §7) —
    this function does not commit."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {_qualified(schema)} (entity, source_id, target_id)
            VALUES (%s, %s, %s)
            ON CONFLICT (entity, source_id) DO UPDATE SET target_id = EXCLUDED.target_id
            """,
            (entity, source_id, target_id),
        )


def get(
    conn: psycopg.Connection, entity: str, source_id: str, *, schema: str = DEFAULT_SCHEMA_NAME
) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT target_id FROM {_qualified(schema)} WHERE entity = %s AND source_id = %s",
            (entity, source_id),
        )
        row = cur.fetchone()
        return row[0] if row else None


def is_loaded(
    conn: psycopg.Connection, entity: str, source_id: str, *, schema: str = DEFAULT_SCHEMA_NAME
) -> bool:
    """Used by resume logic (PRD §7 checkpoint/resume) to skip documents
    already recorded in a prior, interrupted run."""
    return get(conn, entity, source_id, schema=schema) is not None


def has_any(conn: psycopg.Connection, entity: str, *, schema: str = DEFAULT_SCHEMA_NAME) -> bool:
    """Whether *any* id_map row exists for this entity at all — distinct
    from `get()` missing one specific source_id. Used by migrate/load.py to
    tell "this individual reference is dangling" apart from "the entity
    this looks up hasn't loaded a single row yet" (near-certainly a load-
    order bug, or a referenced external run that never happened) before
    applying an `on_missing` policy — a policy for dangling references is
    not a correct answer to "wrong order", and silently absorbing that
    distinction would turn a loud ordering bug into a quiet all-NULL
    column. `entity` is the leading column of the primary key, so this is
    an indexed lookup, not a table scan."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT 1 FROM {_qualified(schema)} WHERE entity = %s LIMIT 1", (entity,))
        return cur.fetchone() is not None
