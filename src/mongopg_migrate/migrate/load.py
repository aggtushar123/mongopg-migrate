"""The real batch loader: entity-ordered COPY with `_mongopg.id_map`-based
ID remapping and per-batch checkpoint/resume.

PRD §6 step 6 / §7 / §8. Sequencing uses `MappingFile.entity_load_order()`
(dependency graph derived from `lookup:` references in the mapping itself),
not `PostgresSchema.load_order()` — the latter is a table-level FK graph
and doesn't see that e.g. `orders.explode.items.fields.productId` depends
on `products` even though the `orders` table has no direct FK to it (only
`order_items` does). `PostgresSchema.load_order()` remains what dry-run
uses to check for genuine Postgres-level FK cycles (PRD §10).

Each batch of Mongo documents for one entity is COPYed into its target
table, its `explode`/`junction` child rows are COPYed into theirs, and the
batch's `_mongopg.id_map` rows and `_mongopg.load_checkpoint` cursor are
written — all in one Postgres transaction, then committed together. A kill
mid-run resumes from the last committed checkpoint (`find({"_id": {"$gt":
last_source_id}})`, since Mongo ObjectIds sort by creation time) without
re-loading anything already committed.

`--mode truncate|append|upsert` (PRD §7 — `truncate`/`append` P0, `upsert`
P1). The CLI's `--mode` flag has no default (PRD §6 step 6: truncate must
never be silently assumed on a non-empty target) — this module trusts that
an explicit choice already reached it.

`upsert` (PRD §7: "COPY into a staging table, then INSERT ... ON CONFLICT
(pk) DO UPDATE") applies to the main entity table and to `junction` tables,
both of which have a natural conflict target (the entity's own PK; the
junction's two FK columns, which must have a matching unique constraint —
Postgres errors loudly if not, which is correct: that's a real schema gap,
not something to paper over). `explode` child tables always plain-COPY
regardless of mode: their PK is a synthetic `SERIAL` with no natural key
derivable from the source document to conflict on, so there is nothing
honest to upsert against — re-running `upsert` re-inserts new child rows
exactly like `append` would (checkpoint/resume already prevents this from
duplicating within one logical run; a deliberate second run over the same
already-loaded documents is a `truncate` situation, not an `upsert` one).

`entity.unmapped.jsonb` is a real landing, not a label: every field listed
there is serialized into one JSON object and written to
`entity.unmapped.jsonb_column` alongside the mapped fields — see
`_build_jsonb_payload`. (`mapping/schema.py`'s `UnmappedPolicy` validator
already refuses a mapping where `jsonb` is non-empty but `jsonb_column`
isn't set, so this module never has to guess a destination.)

`entity.filter` (PRD §7 P0 "multiple mappings filtered by discriminator")
restricts the Mongo query to one discriminator value — see
`EntityMapping.mongo_filter()`. Merged with the resume cursor's `$gt`
clause; both are plain top-level query keys, so a dict union is enough,
no `$and` needed.

An entity already marked `done` is still re-queried past its checkpointed
`last_source_id` (not skipped outright) — otherwise `append`/`upsert`
would only ever see whatever existed the first time an entity finished,
and picking up documents inserted since would require manually deleting
the checkpoint row. `already_done=True` on the result now means "queried,
found nothing new" rather than "didn't even look".

`mapping.external_databases` (a microservices-split scenario: e.g. a
`bookings` mapping targeting a booking-service database needs `lookup:
hospitals`, where `hospitals` was migrated into a *separate*
hospital-service database) opens one extra read/write connection per
distinct external database — see `open_external_connections()` — and
routes `lookup:` resolution for those specific entities to that
connection's `_mongopg.id_map` instead of this run's own. Every other
entity's lookups, and everything else about the load, are unaffected.
"""

from __future__ import annotations

import itertools
import os
import uuid
from dataclasses import dataclass, field

import psycopg
from psycopg.types.json import Jsonb
from pymongo import MongoClient
from pymongo.database import Database

from mongopg_migrate.introspect.postgres import PostgresSchema
from mongopg_migrate.mapping.schema import EntityMapping, FieldSpec, MappingFile
from mongopg_migrate.migrate import checkpoint, idmap
from mongopg_migrate.migrate.idstrategy import resolve_new_id
from mongopg_migrate.migrate.transform import apply_default, apply_transform, get_nested, json_safe

DEFAULT_BATCH_SIZE = 500
SUPPORTED_MODES = ("truncate", "append", "upsert")


class LoadError(Exception):
    pass


@dataclass
class EntityLoadResult:
    entity: str
    rows_loaded: int
    resumed_from: str | None
    already_done: bool = False


@dataclass
class LoadSummary:
    mode: str
    results: list[EntityLoadResult] = field(default_factory=list)


def _column_type(pg_schema: PostgresSchema, table: str, column: str) -> str:
    return pg_schema.tables[table].columns[column].data_type.lower()


def _cast_for_column(value_str: str, data_type: str):
    if "uuid" in data_type:
        return uuid.UUID(value_str)
    if data_type in ("integer", "bigint", "smallint") or "serial" in data_type:
        return int(value_str)
    return value_str


def _resolve_lookup(
    conn: psycopg.Connection,
    lookup_entity: str,
    source_value: object,
    target_table: str,
    target_column: str,
    pg_schema: PostgresSchema,
    *,
    internal_schema: str = idmap.DEFAULT_SCHEMA_NAME,
    external_conns: dict[str, psycopg.Connection] | None = None,
):
    if source_value is None:
        return None
    source_id_str = str(source_value)
    # A cross-database external entity (mapping.external_databases) is checked
    # on its own connection, in its own database's id_map — under that
    # database's real, default `_mongopg` schema, NEVER this run's own
    # `internal_schema`. `internal_schema` only means something for *this*
    # run's own local bookkeeping (Layer B's disposable pass renames it to a
    # throwaway schema) — the external database is a separate, independently
    # migrated database whose id_map always lives at the standard name,
    # regardless of what this particular run happens to call its own.
    if lookup_entity in (external_conns or {}):
        lookup_conn = external_conns[lookup_entity]
        lookup_schema = idmap.DEFAULT_SCHEMA_NAME
    else:
        lookup_conn = conn
        lookup_schema = internal_schema
    target_id_str = idmap.get(lookup_conn, lookup_entity, source_id_str, schema=lookup_schema)
    if target_id_str is None:
        raise LoadError(
            f"lookup miss: no {lookup_schema}.id_map row for entity={lookup_entity!r} "
            f"source_id={source_id_str!r} (needed for {target_table}.{target_column}) — "
            f"was {lookup_entity!r} loaded first? See MappingFile.entity_load_order()."
        )
    return _cast_for_column(target_id_str, _column_type(pg_schema, target_table, target_column))


def _resolve_field_value(
    doc: dict,
    key: str,
    fspec: FieldSpec,
    *,
    context: str,
    conn: psycopg.Connection,
    pg_schema: PostgresSchema,
    target_table: str,
    internal_schema: str = idmap.DEFAULT_SCHEMA_NAME,
    external_conns: dict[str, psycopg.Connection] | None = None,
):
    raw = get_nested(doc, key)
    if fspec.lookup:
        value = _resolve_lookup(
            conn,
            fspec.lookup,
            raw,
            target_table,
            fspec.target,
            pg_schema,
            internal_schema=internal_schema,
            external_conns=external_conns,
        )
    else:
        value = apply_transform(fspec.transform, raw)
    value = apply_default(fspec.transform, value)

    if value is None:
        col = pg_schema.tables.get(target_table, None)
        col_info = col.columns.get(fspec.target) if col else None
        if col_info is not None and not col_info.is_nullable:
            raise LoadError(
                f"{context}.{key}: null value for NOT NULL column {target_table}.{fspec.target} "
                "(source field missing/null and no `default:` transform set)"
            )
    return value


def _build_jsonb_payload(doc: dict, jsonb_fields: list[str]) -> Jsonb:
    return Jsonb({f: json_safe(get_nested(doc, f)) for f in jsonb_fields})


def _copy_rows(conn: psycopg.Connection, table: str, columns: list[str], rows: list[tuple]) -> None:
    if not rows:
        return
    col_list = ", ".join(f'"{c}"' for c in columns)
    with conn.cursor() as cur, cur.copy(f'COPY "{table}" ({col_list}) FROM STDIN') as copy:
        for row in rows:
            copy.write_row(row)


def _upsert_rows(
    conn: psycopg.Connection, table: str, columns: list[str], rows: list[tuple], conflict_columns: list[str]
) -> None:
    """PRD §7 `upsert`: COPY into a per-connection TEMP staging table, then
    `INSERT ... SELECT ... ON CONFLICT (conflict_columns) DO UPDATE` the
    non-key columns (or `DO NOTHING` if there are none — the junction-table
    case, where the two FK columns are the entire row). The staging table
    is a real TEMP table (session-scoped, auto-dropped at connection close)
    so this gets COPY's bulk-load performance instead of row-by-row INSERT,
    while still getting ON CONFLICT semantics that COPY itself can't do.
    """
    if not rows:
        return
    staging = f"__mongopg_stage_{table}"
    col_list = ", ".join(f'"{c}"' for c in columns)
    with conn.cursor() as cur:
        cur.execute(f'CREATE TEMP TABLE IF NOT EXISTS "{staging}" (LIKE "{table}" INCLUDING DEFAULTS)')
        cur.execute(f'TRUNCATE "{staging}"')
    with conn.cursor() as cur, cur.copy(f'COPY "{staging}" ({col_list}) FROM STDIN') as copy:
        for row in rows:
            copy.write_row(row)

    conflict_list = ", ".join(f'"{c}"' for c in conflict_columns)
    update_cols = [c for c in columns if c not in conflict_columns]
    conflict_action = (
        "UPDATE SET " + ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in update_cols) if update_cols else "NOTHING"
    )
    with conn.cursor() as cur:
        cur.execute(
            f'INSERT INTO "{table}" ({col_list}) SELECT {col_list} FROM "{staging}" '
            f"ON CONFLICT ({conflict_list}) DO {conflict_action}"
        )


def open_external_connections(mapping: MappingFile) -> dict[str, psycopg.Connection]:
    """One connection per distinct database named in `mapping.external_databases`
    (deduplicated by DSN — two external entities pointing at the same env var
    share one connection), keyed by entity name for `_resolve_lookup`'s lookup.
    Raises LoadError (closing whatever it already opened first) if a named
    env var isn't set — fail before any Mongo read or Postgres write, not
    partway through a batch.
    """
    dsn_conns: dict[str, psycopg.Connection] = {}
    entity_conns: dict[str, psycopg.Connection] = {}
    try:
        for entity_name, env_var in mapping.external_databases.items():
            dsn = os.environ.get(env_var)
            if not dsn:
                raise LoadError(
                    f"external_databases: entity {entity_name!r} names env var {env_var!r}, "
                    "which is not set"
                )
            if dsn not in dsn_conns:
                dsn_conns[dsn] = psycopg.connect(dsn)
            entity_conns[entity_name] = dsn_conns[dsn]
    except Exception:
        close_external_connections(dsn_conns)
        raise
    return entity_conns


def close_external_connections(conns: dict[str, psycopg.Connection]) -> None:
    """Accepts either the entity-keyed dict `open_external_connections()`
    returns or the dsn-keyed one it builds internally — only `.values()`
    matters, deduplicated by identity so a connection shared by two entities
    isn't closed twice."""
    seen: set[int] = set()
    for conn in conns.values():
        if id(conn) not in seen:
            seen.add(id(conn))
            conn.close()


def _mapped_tables(mapping: MappingFile) -> set[str]:
    tables: set[str] = set()
    for entity in mapping.entities.values():
        tables.add(entity.target)
        tables.update(exp.target for exp in entity.explode.values())
        tables.update(junc.target for junc in entity.junction.values())
    return tables


def _truncate_mapped_tables(conn: psycopg.Connection, mapping: MappingFile) -> None:
    """Truncate only the tables this mapping actually writes to — all in
    one TRUNCATE statement, which is what Postgres requires: it refuses to
    truncate a table with incoming FKs unless every referencing table is
    truncated in that *same* command (ordering separate statements doesn't
    satisfy this, even children-before-parents). Never CASCADE, so a
    foreign key from an out-of-scope table into a mapped table surfaces as
    a loud error instead of silently deleting data this mapping doesn't own.
    """
    mapped = sorted(_mapped_tables(mapping))
    if not mapped:
        return
    table_list = ", ".join(f'"{t}"' for t in mapped)
    with conn.cursor() as cur:
        cur.execute(f"TRUNCATE TABLE {table_list} RESTART IDENTITY")


def _load_entity_batches(
    conn: psycopg.Connection,
    coll,
    entity_name: str,
    entity: EntityMapping,
    pg_schema: PostgresSchema,
    batch_size: int,
    *,
    mode: str = "append",
    internal_schema: str = idmap.DEFAULT_SCHEMA_NAME,
    external_conns: dict[str, psycopg.Connection] | None = None,
) -> EntityLoadResult:
    # Local import: keeps bson out of modules that don't need Mongo types.
    from bson.errors import InvalidId
    from bson.objectid import ObjectId

    # Persists across every batch of this entity's load (not reset per
    # batch) — an int_sequence id_strategy reserves a block of ids per
    # generate_series() round trip instead of one nextval() per document.
    id_buffer: dict[str, list[int]] = {}

    table = entity.target
    id_col = entity.id_strategy.target_field
    id_col_default = None
    table_schema = pg_schema.tables.get(table)
    if table_schema is not None and id_col in table_schema.columns:
        id_col_default = table_schema.columns[id_col].default

    field_keys = list(entity.fields.keys())
    main_columns = [id_col] + [entity.fields[k].target for k in field_keys]

    jsonb_fields = sorted(entity.unmapped.jsonb)
    if jsonb_fields:
        jsonb_column = entity.unmapped.jsonb_column  # required by UnmappedPolicy validation whenever jsonb is non-empty
        if table_schema is None or jsonb_column not in table_schema.columns:
            raise LoadError(
                f"{entity_name}: unmapped.jsonb_column {jsonb_column!r} is not a column on "
                f"{table!r} — fix the mapping file before running migrate"
            )
        main_columns = main_columns + [jsonb_column]

    explode_columns = {
        ename: [exp.parent_fk.target_field] + [exp.fields[k].target for k in exp.fields]
        for ename, exp in entity.explode.items()
    }
    junction_columns = {
        jname: [junc.parent_fk.target_field, junc.child_fk.target_field]
        for jname, junc in entity.junction.items()
    }

    cp = checkpoint.get(conn, entity_name, schema=internal_schema)
    was_previously_done = cp is not None and cp.status == "done"
    resume_from = cp.last_source_id if cp else None

    query: dict = dict(entity.mongo_filter())
    if resume_from:
        try:
            query["_id"] = {"$gt": ObjectId(resume_from)}
        except InvalidId:
            # id_strategy isn't objectid-based (e.g. source _id is a plain string) —
            # compare on the raw checkpointed value instead.
            query["_id"] = {"$gt": resume_from}

    cursor = coll.find(query).sort("_id", 1)
    total_rows = 0

    while True:
        batch = list(itertools.islice(cursor, batch_size))
        if not batch:
            break

        main_rows: list[tuple] = []
        explode_rows: dict[str, list[tuple]] = {k: [] for k in explode_columns}
        junction_rows: dict[str, list[tuple]] = {k: [] for k in junction_columns}
        idmap_entries: list[tuple[str, str, str]] = []
        last_id_in_batch = None

        for doc in batch:
            source_id = doc["_id"]
            resolved = resolve_new_id(
                entity.id_strategy, source_id, conn=conn, column_default=id_col_default, id_buffer=id_buffer
            )

            row = [resolved.column_value]
            for k in field_keys:
                row.append(
                    _resolve_field_value(
                        doc,
                        k,
                        entity.fields[k],
                        context=entity_name,
                        conn=conn,
                        pg_schema=pg_schema,
                        target_table=table,
                        internal_schema=internal_schema,
                        external_conns=external_conns,
                    )
                )
            if jsonb_fields:
                row.append(_build_jsonb_payload(doc, jsonb_fields))
            main_rows.append(tuple(row))

            for ename, exp in entity.explode.items():
                for item in doc.get(ename) or []:
                    erow = [resolved.column_value]
                    for k, fspec in exp.fields.items():
                        erow.append(
                            _resolve_field_value(
                                item,
                                k,
                                fspec,
                                context=f"{entity_name}.{ename}",
                                conn=conn,
                                pg_schema=pg_schema,
                                target_table=exp.target,
                                internal_schema=internal_schema,
                                external_conns=external_conns,
                            )
                        )
                    explode_rows[ename].append(tuple(erow))

            for jname, junc in entity.junction.items():
                for child_source in doc.get(jname) or []:
                    if junc.child_fk.lookup:
                        child_val = _resolve_lookup(
                            conn,
                            junc.child_fk.lookup,
                            child_source,
                            junc.target,
                            junc.child_fk.target_field,
                            pg_schema,
                            internal_schema=internal_schema,
                            external_conns=external_conns,
                        )
                    else:
                        child_val = child_source
                    junction_rows[jname].append((resolved.column_value, child_val))

            idmap_entries.append((entity_name, str(source_id), resolved.str_form))
            last_id_in_batch = source_id

        if mode == "upsert":
            _upsert_rows(conn, table, main_columns, main_rows, conflict_columns=[id_col])
        else:
            _copy_rows(conn, table, main_columns, main_rows)

        # explode children always plain-COPY regardless of mode — see module
        # docstring: a SERIAL child id has no natural conflict target.
        for ename, cols in explode_columns.items():
            _copy_rows(conn, entity.explode[ename].target, cols, explode_rows[ename])

        for jname, junc in entity.junction.items():
            cols = junction_columns[jname]
            if mode == "upsert":
                _upsert_rows(
                    conn,
                    junc.target,
                    cols,
                    junction_rows[jname],
                    conflict_columns=[junc.parent_fk.target_field, junc.child_fk.target_field],
                )
            else:
                _copy_rows(conn, junc.target, cols, junction_rows[jname])
        for e, s, t in idmap_entries:
            idmap.put(conn, e, s, t, schema=internal_schema)
        checkpoint.advance(conn, entity_name, str(last_id_in_batch), len(batch), schema=internal_schema)
        conn.commit()

        total_rows += len(batch)

    checkpoint.mark_done(conn, entity_name, schema=internal_schema)
    conn.commit()
    return EntityLoadResult(
        entity=entity_name,
        rows_loaded=total_rows,
        resumed_from=resume_from,
        already_done=(was_previously_done and total_rows == 0),
    )


def load(
    mapping: MappingFile,
    mongo_uri: str,
    postgres_dsn: str,
    pg_schema: PostgresSchema,
    *,
    mode: str = "truncate",
    batch_size: int = DEFAULT_BATCH_SIZE,
    internal_schema: str = idmap.DEFAULT_SCHEMA_NAME,
    search_path: str | None = None,
) -> LoadSummary:
    """`internal_schema` and `search_path` exist for migrate/dryrun.py's
    realistic pass: it points both at disposable, uniquely-named values so a
    dry run never writes into the real `_mongopg.id_map`/`load_checkpoint`
    (which would make a later real `migrate` think entities are already
    loaded) and never writes into the real target tables (`search_path`
    redirects the unqualified table names this module writes to). Regular
    callers should leave both at their defaults.
    """
    if mode not in SUPPORTED_MODES:
        raise ValueError(f"mode={mode!r} not supported — use one of {SUPPORTED_MODES}")

    order = mapping.entity_load_order()  # raises CircularEntityDependencyError

    mongo_client: MongoClient = MongoClient(mongo_uri)
    external_conns = open_external_connections(mapping)
    try:
        db: Database = mongo_client.get_default_database()
        if db is None:
            raise LoadError("MONGO_URI must include a default database")

        with psycopg.connect(postgres_dsn) as conn:
            conn.autocommit = False
            if search_path is not None:
                with conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {search_path}")
            idmap.ensure_schema(conn, schema=internal_schema)
            checkpoint.ensure_schema(conn, schema=internal_schema)
            conn.commit()

            if mode == "truncate":
                _truncate_mapped_tables(conn, mapping)
                for entity_name in order:
                    checkpoint.reset(conn, entity_name, schema=internal_schema)
                conn.commit()

            summary = LoadSummary(mode=mode)
            for entity_name in order:
                entity = mapping.entities[entity_name]
                result = _load_entity_batches(
                    conn,
                    db[entity.source],
                    entity_name,
                    entity,
                    pg_schema,
                    batch_size,
                    mode=mode,
                    internal_schema=internal_schema,
                    external_conns=external_conns,
                )
                summary.results.append(result)
            return summary
    finally:
        mongo_client.close()
        close_external_connections(external_conns)
