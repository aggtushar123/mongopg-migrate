"""Per-entity load checkpoint: `_mongopg.load_checkpoint`.

PRD §7 P0 "Resume granularity": a kill mid-run must not re-COPY a completed
parent table or leave an entity partially loaded without knowing where it
left off. This tracks, per entity, the last source `_id` successfully
committed (Mongo ObjectIds sort by creation time, so `_id` gives a stable
total order to resume a `find().sort("_id", 1)` cursor from) and whether
the entity is fully done.

Written in the same transaction as each batch's table COPY + `id_map` rows
(see migrate/load.py) — PRD §7: "written in the same transaction/checkpoint
as the table load it belongs to."

Schema name is parameterized like migrate/idmap.py, for the same reason:
migrate/dryrun.py's realistic pass must never write "done" into the real
checkpoint table, or a subsequent real `migrate` would skip entities it
never actually loaded.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

DEFAULT_SCHEMA_NAME = "_mongopg"
TABLE_NAME = "load_checkpoint"


def _qualified(schema: str) -> str:
    return f'"{schema}"."{TABLE_NAME}"'


def ddl(schema: str = DEFAULT_SCHEMA_NAME) -> str:
    qualified = _qualified(schema)
    return f"""
CREATE SCHEMA IF NOT EXISTS "{schema}";

CREATE TABLE IF NOT EXISTS {qualified} (
    entity          TEXT PRIMARY KEY,
    last_source_id  TEXT,
    status          TEXT NOT NULL DEFAULT 'in_progress',
    rows_loaded     BIGINT NOT NULL DEFAULT 0,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


@dataclass
class CheckpointState:
    last_source_id: str | None
    status: str  # "in_progress" | "done"
    rows_loaded: int


def ensure_schema(conn: psycopg.Connection, *, schema: str = DEFAULT_SCHEMA_NAME) -> None:
    with conn.cursor() as cur:
        cur.execute(ddl(schema))


def get(conn: psycopg.Connection, entity: str, *, schema: str = DEFAULT_SCHEMA_NAME) -> CheckpointState | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT last_source_id, status, rows_loaded FROM {_qualified(schema)} WHERE entity = %s",
            (entity,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return CheckpointState(last_source_id=row[0], status=row[1], rows_loaded=row[2])


def advance(
    conn: psycopg.Connection,
    entity: str,
    last_source_id: str,
    rows_delta: int,
    *,
    schema: str = DEFAULT_SCHEMA_NAME,
) -> None:
    """Record progress after a successfully committed batch. Callers must
    call this inside the same transaction as the batch's table COPY."""
    qualified = _qualified(schema)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {qualified} (entity, last_source_id, status, rows_loaded)
            VALUES (%s, %s, 'in_progress', %s)
            ON CONFLICT (entity) DO UPDATE SET
                last_source_id = EXCLUDED.last_source_id,
                rows_loaded = {qualified}.rows_loaded + EXCLUDED.rows_loaded,
                updated_at = now()
            """,
            (entity, last_source_id, rows_delta),
        )


def mark_done(conn: psycopg.Connection, entity: str, *, schema: str = DEFAULT_SCHEMA_NAME) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {_qualified(schema)} (entity, status, rows_loaded)
            VALUES (%s, 'done', 0)
            ON CONFLICT (entity) DO UPDATE SET status = 'done', updated_at = now()
            """,
            (entity,),
        )


def reset(conn: psycopg.Connection, entity: str, *, schema: str = DEFAULT_SCHEMA_NAME) -> None:
    """Used by --mode truncate: a fresh load discards any prior checkpoint
    for the entity so it doesn't skip rows that no longer exist."""
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {_qualified(schema)} WHERE entity = %s", (entity,))
