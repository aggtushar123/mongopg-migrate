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

import weakref

import psycopg

DEFAULT_SCHEMA_NAME = "_mongopg"
TABLE_NAME = "id_map"


def _qualified(schema: str) -> str:
    return f'"{schema}"."{TABLE_NAME}"'


# ── Prefetch cache ───────────────────────────────────────────────────────────
# `get()` is one network round trip, and `_resolve_lookup` calls it once per
# `lookup:` field per row. Over a VPN to RDS that is the dominant cost of every
# wave after the parent table: `empanelment_status_details` (64670 rows) and
# `hospital_details` (41793) each resolve `lookup: hospitals` on every single
# row, so the load rate collapses to roughly one row per round trip.
#
# `prefetch()` reads one entity's whole map in a single query and keeps it here;
# `get()` then answers from memory. It is deliberately OPT-IN — `get()` keeps
# its original single-row behaviour for any (conn, schema, entity) that was
# never prefetched, so callers that have no batch context (report/validate.py,
# migrate/dryrun.py) and the existing tests are untouched.
#
# Safe because of load order, not luck: `entity_load_order()` guarantees an
# entity is fully loaded and committed before anything that looks it up starts,
# and a self-lookup is rejected as a circular dependency. So a snapshot taken
# when the first dependent begins cannot go stale underneath it. `put()`/
# `put_many()` additionally write through to any live snapshot, which covers
# the remaining case of a read-back during an entity's own load.
#
# Keyed on a weak reference to the connection: cache lifetime follows the
# connection's, with no risk of an `id()` being reused by a later object.
_PREFETCHED: weakref.WeakKeyDictionary[psycopg.Connection, dict[tuple[str, str], dict[str, str]]] = (
    weakref.WeakKeyDictionary()
)


def prefetch(conn: psycopg.Connection, entity: str, *, schema: str = DEFAULT_SCHEMA_NAME) -> int:
    """Load one entity's entire id_map into memory for this connection.

    Returns the number of rows cached. Re-prefetching replaces the snapshot.
    """
    with conn.cursor() as cur:
        cur.execute(f"SELECT source_id, target_id FROM {_qualified(schema)} WHERE entity = %s", (entity,))
        rows = cur.fetchall()
    _PREFETCHED.setdefault(conn, {})[(schema, entity)] = {str(s): str(t) for s, t in rows}
    return len(rows)


def is_prefetched(conn: psycopg.Connection, entity: str, *, schema: str = DEFAULT_SCHEMA_NAME) -> bool:
    return (schema, entity) in _PREFETCHED.get(conn, {})


def clear_prefetch(conn: psycopg.Connection | None = None) -> None:
    """Drop cached snapshots — for one connection, or all of them."""
    if conn is None:
        _PREFETCHED.clear()
    else:
        _PREFETCHED.pop(conn, None)


def _write_through(conn: psycopg.Connection, entries: list[tuple[str, str, str]], schema: str) -> None:
    """Keep any live snapshot consistent with rows just written."""
    cached = _PREFETCHED.get(conn)
    if not cached:
        return
    for entity, source_id, target_id in entries:
        snapshot = cached.get((schema, entity))
        if snapshot is not None:
            snapshot[str(source_id)] = str(target_id)


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
    _write_through(conn, [(entity, source_id, target_id)], schema)


# Rows per INSERT in put_many. Each row costs 3 placeholders and Postgres caps a
# statement at 65535 of them, so 5000 (15000 placeholders) stays well inside the
# limit whatever batch size the caller was given.
_PUT_MANY_CHUNK = 5000


def put_many(
    conn: psycopg.Connection, entries: list[tuple[str, str, str]], *, schema: str = DEFAULT_SCHEMA_NAME
) -> None:
    """Record many ID remappings in one round trip per chunk.

    Semantically identical to calling `put()` in a loop, and like `put()` it
    does not commit — the caller still owns the transaction boundary that keeps
    these rows atomic with the table load they belong to (PRD §7).

    It exists because the loop version is one network round trip PER ROW. On
    localhost that is invisible; across a VPN to RDS it is the entire runtime.
    Measured on the hospital wave over the tunnel: ~1000 rows/min, with the
    connection sitting `idle in transaction` on

        INSERT INTO "_mongopg"."id_map" ... VALUES ($1, $2, $3)

    between every row. The COPY that loads the actual table was already
    batched; this was the one per-row statement left, so it set the pace for
    the whole migration. Raising `--batch-size` does NOT help — the inserts are
    per row regardless, and a bigger batch only delays each checkpoint.

    Duplicate keys within one call: the ON CONFLICT clause cannot see rows
    inserted by the same statement, so a repeated (entity, source_id) inside a
    single chunk would raise
        ON CONFLICT DO UPDATE command cannot affect row a second time
    rather than last-write-wins as the loop did. Callers pass one entry per
    document per entity, so this cannot arise from a well-formed mapping — but
    de-duplicating here keeps the two functions interchangeable instead of
    turning a mapping bug into a confusing Postgres error.
    """
    if not entries:
        return

    deduped: dict[tuple[str, str], tuple[str, str, str]] = {}
    for entity, source_id, target_id in entries:
        deduped[(entity, source_id)] = (entity, source_id, target_id)
    rows = list(deduped.values())

    qualified = _qualified(schema)
    with conn.cursor() as cur:
        for start in range(0, len(rows), _PUT_MANY_CHUNK):
            chunk = rows[start : start + _PUT_MANY_CHUNK]
            values = ", ".join(["(%s, %s, %s)"] * len(chunk))
            params: list[str] = []
            for entity, source_id, target_id in chunk:
                params += [entity, source_id, target_id]
            cur.execute(
                f"""
                INSERT INTO {qualified} (entity, source_id, target_id)
                VALUES {values}
                ON CONFLICT (entity, source_id) DO UPDATE SET target_id = EXCLUDED.target_id
                """,
                params,
            )
    _write_through(conn, rows, schema)


def get(
    conn: psycopg.Connection, entity: str, source_id: str, *, schema: str = DEFAULT_SCHEMA_NAME
) -> str | None:
    # Served from memory when this entity was prefetched for this connection.
    # A prefetched snapshot is authoritative: a miss here is a real miss, not a
    # stale one (see the _PREFETCHED comment above on why load order makes that
    # true), so it must NOT fall through to a query — doing so would put the
    # per-row round trip straight back for exactly the dangling references that
    # `on_missing` exists to handle.
    snapshot = _PREFETCHED.get(conn, {}).get((schema, entity))
    if snapshot is not None:
        return snapshot.get(str(source_id))
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
