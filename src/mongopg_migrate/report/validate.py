"""Post-migration validation: count diff + hashed-field sample diff.

PRD §6 step 7 / §7 / §9: "row counts matching is necessary but not
sufficient; values must be checked too."

1. Count diff: per entity, `count_documents` on the Mongo collection vs
   `count(*)` on the target Postgres table — and, since an `explode`/
   `junction` field's document count isn't the same thing as its row count,
   a summed array-length aggregate on the source field vs `count(*)` on the
   child/junction table.
2. Hashed-field sample diff: for a random sample of rows already recorded
   in `_mongopg.id_map` for an entity, re-fetch the source Mongo document,
   recompute every mapped field's value exactly as migrate/load.py would
   (same transform, same `lookup:` resolution against id_map), and compare
   a hash of the recomputed row to a hash of the actual Postgres row. A
   hash mismatch triggers a per-field comparison so the report says exactly
   which column is wrong, not just that something is — this is what catches
   transform bugs, truncated types, and silent coercion that a count alone
   would miss (PRD §9 "Zero silent data loss").

The sample diff also covers `unmapped.jsonb` (PRD §9 zero-silent-data-loss
applies to the jsonb landing too, not just mapped columns): the same
`migrate.transform.json_safe()` used to build the payload at load time
recomputes it here from the source document, compared against whatever
Postgres actually has in `unmapped.jsonb_column`.

Known, stated scope limit (no silent narrowing): the sample diff checks
each entity's own mapped fields and its jsonb payload, not `explode`/
`junction` child rows — count diff already covers those at the row-count
level; per-field sampling of child rows is a reasonable future extension,
not done here.

`mapping.external_databases` (a `lookup:` whose entity lives in a
*different* Postgres database — see migrate/load.py's module docstring)
is resolved the same way here as at load time: `open_external_connections()`
opens one connection per distinct external database, and
`_recompute_field_value` checks the right one instead of always assuming
`postgres_dsn`'s own `_mongopg.id_map`.
"""

from __future__ import annotations

import datetime
import hashlib
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import psycopg
from pymongo import MongoClient
from pymongo.database import Database

from mongopg_migrate.introspect.postgres import PostgresSchema
from mongopg_migrate.mapping.schema import (
    EntityMapping,
    ExplodeSpec,
    FieldSpec,
    MappingFile,
    OnMissing,
    UnpivotSpec,
)
from mongopg_migrate.migrate import idmap
from mongopg_migrate.migrate.load import (
    LoadError,
    close_external_connections,
    open_external_connections,
)
from mongopg_migrate.migrate.transform import apply_default, apply_transform, get_nested, json_safe

DEFAULT_SAMPLE_SIZE = 200


@dataclass
class CountDiff:
    entity: str
    table: str
    mongo_count: int
    postgres_count: int
    # Known, already-reconciled reduction from an on_missing=skip_row policy
    # (0 for anything else — a null policy still writes a row, so it never
    # affects a count). Without this, a deliberate, explicitly-configured
    # drop would report as an unexplained MISMATCH indistinguishable from
    # real data loss — the exact confusion `on_missing` exists to avoid.
    expected_skip: int = 0

    @property
    def matches(self) -> bool:
        return self.postgres_count == self.mongo_count - self.expected_skip


@dataclass
class SampleDiff:
    entity: str
    source_id: str
    mismatched_fields: list[str]


@dataclass
class OnMissingDiff:
    """Independently re-derived, right now, from live Mongo + id_map — not
    a copy of load.py's own in-memory tally (which is per-run and gone once
    the process exits). Purely informational: a nonzero dangling_count is
    the policy working as configured, not a failure — it never affects
    ValidationReport.ok. Exists so 'every miss is counted and reported' is
    true at validate time too, not just in migrate's own output, and so it
    catches drift between migrate-time and validate-time (e.g. the
    referenced entity was deleted after a successful migration)."""

    entity: str
    field: str
    policy: str  # "null" | "skip_row"
    dangling_count: int


@dataclass
class ValidationReport:
    count_diffs: list[CountDiff] = field(default_factory=list)
    sample_diffs: list[SampleDiff] = field(default_factory=list)
    sampled_rows: int = 0
    on_missing_diffs: list[OnMissingDiff] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.matches for c in self.count_diffs) and not self.sample_diffs


class ValidationError(Exception):
    pass


# --- count diff ------------------------------------------------------------------


def _table_count(conn: psycopg.Connection, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f'SELECT count(*) FROM "{table}"')
        (n,) = cur.fetchone()
        return n


def _sum_array_length(db: Database, collection: str, field_name: str, mongo_filter: dict) -> int:
    pipeline = [
        *([{"$match": mongo_filter}] if mongo_filter else []),
        {"$project": {"n": {"$size": {"$ifNull": [f"${field_name}", []]}}}},
        {"$group": {"_id": None, "total": {"$sum": "$n"}}},
    ]
    result = list(db[collection].aggregate(pipeline))
    return result[0]["total"] if result else 0


def _flatten_explode(explode: dict[str, ExplodeSpec], *, path_prefix: str = "") -> list[tuple[str, ExplodeSpec]]:
    # Mirrors migrate/load.py's / migrate/dryrun.py's private helper of the
    # same name (parent-before-child pre-order) — kept as a separate small
    # copy rather than importing a leading-underscore name across modules.
    out: list[tuple[str, ExplodeSpec]] = []
    for ename, exp in explode.items():
        path = f"{path_prefix}.{ename}" if path_prefix else ename
        out.append((path, exp))
        out.extend(_flatten_explode(exp.explode, path_prefix=path))
    return out


def _sum_nested_array_length(db: Database, collection: str, path: str, mongo_filter: dict) -> int:
    """Row count for a (possibly nested) explode path, e.g.
    "facilities.categoryParts" — one `$unwind` per path segment, so a
    document missing (or with an empty) array at any level simply
    contributes 0 rather than erroring, matching `_sum_array_length`'s
    treatment of a top-level missing array."""
    segments = path.split(".")
    pipeline = [*([{"$match": mongo_filter}] if mongo_filter else [])]
    for i in range(len(segments)):
        pipeline.append({"$unwind": f"${'.'.join(segments[: i + 1])}"})
    pipeline.append({"$count": "n"})
    result = list(db[collection].aggregate(pipeline))
    return result[0]["n"] if result else 0


def _sum_unpivot_rows(db: Database, collection: str, unp: UnpivotSpec, mongo_filter: dict) -> int:
    """Mirrors load.py's per-item skip_null check: an item with skip_null=True
    (the spec-level default, applied per item) contributes a row only when its
    source field is present and non-null; skip_null=False contributes one row
    per matching document regardless of that field's value."""
    total = 0
    for item in unp.items:
        if unp.skip_null:
            pipeline = [
                *([{"$match": mongo_filter}] if mongo_filter else []),
                {"$match": {item.source_field: {"$ne": None}}},
                {"$count": "n"},
            ]
            result = list(db[collection].aggregate(pipeline))
            total += result[0]["n"] if result else 0
        else:
            total += db[collection].count_documents(mongo_filter)
    return total


def _skip_row_reduction(
    db: Database,
    conn: psycopg.Connection,
    entity: EntityMapping,
    fields: dict,
    *,
    explode_path: str | None,
    mongo_filter: dict,
    internal_schema: str,
    external_conns: dict[str, psycopg.Connection] | None,
) -> int:
    """Sum of dangling-reference counts across every on_missing=skip_row
    field in `fields` (an entity's own `.fields`, or one explode level's
    `.fields`). Known limitation, stated rather than hidden: if a single
    row has more than one independently-dangling skip_row field, each is
    counted here but the row is only ever actually dropped once — this can
    over-subtract in that (rare — most mappings have at most one lookup
    field prone to going dangling) case. A junction's skip_row reduction
    doesn't share this risk (one child_fk per row) and isn't computed
    here — see the junction branch in _count_diffs."""
    total = 0
    for key, fspec in fields.items():
        if not fspec.lookup or fspec.on_missing != OnMissing.SKIP_ROW:
            continue
        if fspec.lookup in (external_conns or {}):
            id_map_conn, id_map_schema = external_conns[fspec.lookup], idmap.DEFAULT_SCHEMA_NAME
        else:
            id_map_conn, id_map_schema = conn, internal_schema
        known = _known_source_ids(id_map_conn, fspec.lookup, schema=id_map_schema)
        present = _mongo_present_scalar_values(db, entity.source, explode_path, key, mongo_filter)
        total += len(present - known)
    return total


def _count_diffs(
    db: Database,
    conn: psycopg.Connection,
    mapping: MappingFile,
    *,
    internal_schema: str = idmap.DEFAULT_SCHEMA_NAME,
    external_conns: dict[str, psycopg.Connection] | None = None,
) -> list[CountDiff]:
    diffs: list[CountDiff] = []
    for name, entity in mapping.entities.items():
        # entity.mongo_filter() matters here specifically: without it, a
        # discriminator-filtered entity (PRD §7 P0) would count every OTHER
        # filtered entity's documents from the same collection too — a
        # count_documents({}) that looks fine but is comparing the wrong
        # numbers.
        mongo_filter = entity.mongo_filter()
        entity_skip = _skip_row_reduction(
            db, conn, entity, entity.fields, explode_path=None, mongo_filter=mongo_filter,
            internal_schema=internal_schema, external_conns=external_conns,
        )
        diffs.append(
            CountDiff(
                entity=name,
                table=entity.target,
                mongo_count=db[entity.source].count_documents(mongo_filter),
                postgres_count=_table_count(conn, entity.target),
                expected_skip=entity_skip,
            )
        )
        for path, exp in _flatten_explode(entity.explode):
            explode_skip = _skip_row_reduction(
                db, conn, entity, exp.fields, explode_path=path, mongo_filter=mongo_filter,
                internal_schema=internal_schema, external_conns=external_conns,
            )
            diffs.append(
                CountDiff(
                    entity=f"{name}.{path}",
                    table=exp.target,
                    mongo_count=_sum_nested_array_length(db, entity.source, path, mongo_filter),
                    postgres_count=_table_count(conn, exp.target),
                    expected_skip=explode_skip,
                )
            )
        for jname, junc in entity.junction.items():
            junction_skip = 0
            if junc.child_fk.lookup and junc.on_missing == OnMissing.SKIP_ROW:
                if junc.child_fk.lookup in (external_conns or {}):
                    id_map_conn, id_map_schema = external_conns[junc.child_fk.lookup], idmap.DEFAULT_SCHEMA_NAME
                else:
                    id_map_conn, id_map_schema = conn, internal_schema
                known = _known_source_ids(id_map_conn, junc.child_fk.lookup, schema=id_map_schema)
                present = _mongo_present_array_values(db, entity.source, jname, mongo_filter)
                junction_skip = len(present - known)
            diffs.append(
                CountDiff(
                    entity=f"{name}.{jname}",
                    table=junc.target,
                    mongo_count=_sum_array_length(db, entity.source, jname, mongo_filter),
                    postgres_count=_table_count(conn, junc.target),
                    expected_skip=junction_skip,
                )
            )
        for uname, unp in entity.unpivot.items():
            diffs.append(
                CountDiff(
                    entity=f"{name}.{uname}",
                    table=unp.target,
                    mongo_count=_sum_unpivot_rows(db, entity.source, unp, mongo_filter),
                    postgres_count=_table_count(conn, unp.target),
                )
            )
    return diffs


# --- sample diff ------------------------------------------------------------------


def _canonicalize(value: Any) -> Any:
    """Normalizes a value so equal-but-differently-represented values from
    Mongo/Python vs. Postgres compare (and hash) equal: a `numeric` column
    round-trips as `Decimal`, the recomputed value is often a `float`
    straight from BSON — `Decimal('19.99') == 19.99` is not reliably true
    due to binary float imprecision, but `Decimal(str(19.99))
    == Decimal(str(Decimal('19.99')))` is. `.normalize()` matters too, not
    just for `==`: a `numeric` column's scale can make Postgres return
    `Decimal('19.990000')` where the recomputed value is `Decimal('19.99')`
    — equal by `==`, but `repr()` (what `_row_hash` hashes) differs unless
    both are normalized to the same minimal-coefficient form first. The
    same reasoning extends to dict/list (the `unmapped.jsonb` payload):
    `==` doesn't care about dict key order, but `repr()` does — Postgres's
    jsonb storage does not preserve the original key insertion order, so
    without sorting keys here, a value-identical jsonb blob could still
    hash differently purely from key reordering and look like a mismatch.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value)).normalize()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime.datetime):
        return value.astimezone(datetime.UTC).isoformat()
    if isinstance(value, dict):
        return {k: _canonicalize(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_canonicalize(v) for v in value]
    return value


def _row_hash(values: list) -> str:
    canon = [repr(_canonicalize(v)) for v in values]
    return hashlib.sha256("|".join(canon).encode()).hexdigest()


def _cast_for_column(value_str: str, data_type: str) -> Any:
    if "uuid" in data_type:
        return uuid.UUID(value_str)
    if data_type in ("integer", "bigint", "smallint") or "serial" in data_type:
        return int(value_str)
    return value_str


_LOOKUP_MISSING = object()  # sentinel: distinguishable from a legitimate None


def _recompute_field_value(
    doc: dict,
    key: str,
    fspec: FieldSpec,
    *,
    conn: psycopg.Connection,
    pg_schema: PostgresSchema,
    target_table: str,
    internal_schema: str,
    external_conns: dict[str, psycopg.Connection] | None = None,
) -> Any:
    """Mirrors migrate/load.py's field resolution exactly (same transform,
    same lookup-via-id_map, same external-database routing for a
    `mapping.external_databases` entity, same on_missing policy), but
    read-only: an `on_missing=error` (the default) lookup miss here is
    reported as a mismatch rather than raised, since by validation time the
    row is already loaded — if id_map no longer has the entry, that is
    itself the finding, not a reason to crash. `on_missing=null` returns
    None here too, matching what load.py actually wrote — treating it as
    `_LOOKUP_MISSING` instead would report a false mismatch on every single
    row the policy correctly rescued, defeating the whole point of setting
    it. (`on_missing=skip_row` needs no special case: a skipped document
    never got an id_map entry, so `_sample_diffs`'s sampling — which reads
    straight from id_map — never encounters it in the first place.)"""
    raw = get_nested(doc, key)
    if fspec.lookup:
        if raw is None:
            return None
        # Same rule as migrate/load.py's _resolve_lookup: a cross-database
        # external entity always uses its own database's real, default
        # schema — never this run's own internal_schema.
        if fspec.lookup in (external_conns or {}):
            id_map_conn = external_conns[fspec.lookup]
            id_map_schema = idmap.DEFAULT_SCHEMA_NAME
        else:
            id_map_conn = conn
            id_map_schema = internal_schema
        target_id_str = idmap.get(id_map_conn, fspec.lookup, str(raw), schema=id_map_schema)
        if target_id_str is None:
            return None if fspec.on_missing == OnMissing.NULL else _LOOKUP_MISSING
        col_type = pg_schema.tables[target_table].columns[fspec.target].data_type.lower()
        return _cast_for_column(target_id_str, col_type)
    value = apply_transform(fspec.transform, raw)
    return apply_default(fspec.transform, value)


def _sample_diffs(
    db: Database,
    conn: psycopg.Connection,
    mapping: MappingFile,
    pg_schema: PostgresSchema,
    *,
    sample_size: int,
    internal_schema: str,
    external_conns: dict[str, psycopg.Connection] | None = None,
) -> tuple[list[SampleDiff], int]:
    diffs: list[SampleDiff] = []
    total_sampled = 0

    for name, entity in mapping.entities.items():
        field_keys = list(entity.fields.keys())
        jsonb_fields = sorted(entity.unmapped.jsonb)
        jsonb_column = entity.unmapped.jsonb_column
        if not field_keys and not jsonb_fields:
            continue

        pg_columns = [entity.fields[k].target for k in field_keys]
        if jsonb_fields:
            pg_columns = pg_columns + [jsonb_column]
        pk_col = entity.id_strategy.target_field
        pk_type = pg_schema.tables[entity.target].columns[pk_col].data_type.lower()

        with conn.cursor() as cur:
            cur.execute(
                f'SELECT source_id, target_id FROM "{internal_schema}".id_map '
                "WHERE entity = %s ORDER BY random() LIMIT %s",
                (name, sample_size),
            )
            sample = cur.fetchall()

        for source_id_str, target_id_str in sample:
            total_sampled += 1
            doc = _refetch_mongo_doc(db, entity, source_id_str)
            if doc is None:
                diffs.append(SampleDiff(entity=name, source_id=source_id_str, mismatched_fields=["<document missing>"]))
                continue

            recomputed = [
                _recompute_field_value(
                    doc, k, entity.fields[k], conn=conn, pg_schema=pg_schema,
                    target_table=entity.target, internal_schema=internal_schema,
                    external_conns=external_conns,
                )
                for k in field_keys
            ]
            if jsonb_fields:
                recomputed.append({f: json_safe(get_nested(doc, f)) for f in jsonb_fields})

            col_list = ", ".join(f'"{c}"' for c in pg_columns)
            with conn.cursor() as cur:
                cur.execute(
                    f'SELECT {col_list} FROM "{entity.target}" WHERE "{pk_col}" = %s',
                    (_cast_for_column(target_id_str, pk_type),),
                )
                row = cur.fetchone()
            if row is None:
                diffs.append(SampleDiff(entity=name, source_id=source_id_str, mismatched_fields=["<row missing>"]))
                continue
            actual = list(row)

            if _row_hash(recomputed) == _row_hash(actual):
                continue

            mismatched = [
                field_keys[i]
                for i in range(len(field_keys))
                if not _values_equal(recomputed[i], actual[i])
            ]
            if jsonb_fields and not _values_equal(recomputed[-1], actual[-1]):
                mismatched.append(jsonb_column)
            diffs.append(SampleDiff(entity=name, source_id=source_id_str, mismatched_fields=mismatched))

    return diffs, total_sampled


def _values_equal(a: Any, b: Any) -> bool:
    if a is _LOOKUP_MISSING:
        return False
    return _canonicalize(a) == _canonicalize(b)


def _known_source_ids(conn: psycopg.Connection, lookup_entity: str, *, schema: str) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(f'SELECT source_id FROM "{schema}".id_map WHERE entity = %s', (lookup_entity,))
        return {row[0] for row in cur.fetchall()}


def _mongo_present_scalar_values(
    db: Database, collection: str, explode_path: str | None, field_name: str, mongo_filter: dict
) -> set[str]:
    """All non-null values actually present at `field_name`, across every
    matching document — `explode_path` (e.g. "facilities" or
    "facilities.categoryParts") unwinds through that many nesting levels
    first, so this covers a `lookup:` on an explode-level field too, not
    just a top-level entity field. None for a top-level field."""
    pipeline = [*([{"$match": mongo_filter}] if mongo_filter else [])]
    prefix = ""
    if explode_path:
        segments = explode_path.split(".")
        for i in range(len(segments)):
            pipeline.append({"$unwind": f"${'.'.join(segments[: i + 1])}"})
        prefix = f"{explode_path}."
    full_field = f"{prefix}{field_name}"
    pipeline.append({"$match": {full_field: {"$ne": None}}})
    pipeline.append({"$project": {"v": f"${full_field}"}})
    return {str(d["v"]) for d in db[collection].aggregate(pipeline)}


def _mongo_present_array_values(db: Database, collection: str, field_name: str, mongo_filter: dict) -> set[str]:
    """Every scalar value that appears anywhere in `field_name` (a
    `junction` field — an array of scalar FKs), across all matching
    documents. $unwind rather than $ne-null-and-project: this field is the
    array itself, not a scalar that might be null."""
    pipeline = [
        *([{"$match": mongo_filter}] if mongo_filter else []),
        {"$unwind": f"${field_name}"},
        {"$project": {"v": f"${field_name}"}},
    ]
    return {str(d["v"]) for d in db[collection].aggregate(pipeline)}


def _flatten_explode_for_on_missing(explode: dict, *, path_prefix: str = "") -> list[tuple[str, object]]:
    # Same shape as _flatten_explode above, kept separate because it
    # doesn't need ExplodeSpec's target — just its .fields and .explode.
    out: list[tuple[str, object]] = []
    for ename, exp in explode.items():
        path = f"{path_prefix}.{ename}" if path_prefix else ename
        out.append((path, exp))
        out.extend(_flatten_explode_for_on_missing(exp.explode, path_prefix=path))
    return out


def _count_on_missing(
    db: Database,
    conn: psycopg.Connection,
    mapping: MappingFile,
    *,
    internal_schema: str,
    external_conns: dict[str, psycopg.Connection] | None = None,
) -> list[OnMissingDiff]:
    """Independently re-derives, right now, how many dangling references
    each on_missing-enabled field/junction actually has — see
    OnMissingDiff. Fields with the default on_missing=error are skipped
    entirely: a dangling reference there is either already impossible (it
    would have hard-failed migrate) or is genuinely new drift, which the
    existing sample-diff mismatch path already surfaces via
    _LOOKUP_MISSING — this function is specifically about the policy-
    covered cases, which nothing else reports on at validate time."""
    diffs: list[OnMissingDiff] = []
    for name, entity in mapping.entities.items():
        mongo_filter = entity.mongo_filter()

        def _lookup_conn_and_schema(lookup_entity: str) -> tuple[psycopg.Connection, str]:
            if lookup_entity in (external_conns or {}):
                return external_conns[lookup_entity], idmap.DEFAULT_SCHEMA_NAME
            return conn, internal_schema

        for key, fspec in entity.fields.items():
            if not fspec.lookup or fspec.on_missing == OnMissing.ERROR:
                continue
            id_map_conn, id_map_schema = _lookup_conn_and_schema(fspec.lookup)
            known = _known_source_ids(id_map_conn, fspec.lookup, schema=id_map_schema)
            present = _mongo_present_scalar_values(db, entity.source, None, key, mongo_filter)
            dangling = len(present - known)
            if dangling:
                diffs.append(
                    OnMissingDiff(entity=name, field=f"{name}.{key}", policy=fspec.on_missing.value, dangling_count=dangling)
                )

        for path, exp in _flatten_explode_for_on_missing(entity.explode):
            for key, fspec in exp.fields.items():
                if not fspec.lookup or fspec.on_missing == OnMissing.ERROR:
                    continue
                id_map_conn, id_map_schema = _lookup_conn_and_schema(fspec.lookup)
                known = _known_source_ids(id_map_conn, fspec.lookup, schema=id_map_schema)
                present = _mongo_present_scalar_values(db, entity.source, path, key, mongo_filter)
                dangling = len(present - known)
                if dangling:
                    diffs.append(
                        OnMissingDiff(
                            entity=name, field=f"{name}.{path}.{key}", policy=fspec.on_missing.value,
                            dangling_count=dangling,
                        )
                    )

        for jname, junc in entity.junction.items():
            if not junc.child_fk.lookup or junc.on_missing == OnMissing.ERROR:
                continue
            id_map_conn, id_map_schema = _lookup_conn_and_schema(junc.child_fk.lookup)
            known = _known_source_ids(id_map_conn, junc.child_fk.lookup, schema=id_map_schema)
            present = _mongo_present_array_values(db, entity.source, jname, mongo_filter)
            dangling = len(present - known)
            if dangling:
                diffs.append(
                    OnMissingDiff(
                        entity=name, field=f"{name}.{jname}", policy=junc.on_missing.value, dangling_count=dangling
                    )
                )
    return diffs


def _refetch_mongo_doc(db: Database, entity: EntityMapping, source_id_str: str) -> dict | None:
    from bson.errors import InvalidId
    from bson.objectid import ObjectId

    try:
        query_id: Any = ObjectId(source_id_str)
    except InvalidId:
        query_id = source_id_str
    return db[entity.source].find_one({"_id": query_id})


# --- entry point --------------------------------------------------------------


def validate(
    mapping: MappingFile,
    mongo_uri: str,
    postgres_dsn: str,
    pg_schema: PostgresSchema,
    *,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    internal_schema: str = idmap.DEFAULT_SCHEMA_NAME,
) -> ValidationReport:
    client: MongoClient = MongoClient(mongo_uri)
    try:
        external_conns = open_external_connections(mapping)
    except LoadError as e:
        client.close()
        raise ValidationError(str(e)) from e
    try:
        db: Database = client.get_default_database()
        if db is None:
            raise ValidationError("MONGO_URI must include a default database")

        with psycopg.connect(postgres_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", (internal_schema,)
                )
                if cur.fetchone() is None:
                    raise ValidationError(
                        f'"{internal_schema}" schema not found — has `migrate` been run yet?'
                    )

            count_diffs = _count_diffs(
                db, conn, mapping, internal_schema=internal_schema, external_conns=external_conns,
            )
            sample_diffs, sampled_rows = _sample_diffs(
                db, conn, mapping, pg_schema, sample_size=sample_size, internal_schema=internal_schema,
                external_conns=external_conns,
            )
            on_missing_diffs = _count_on_missing(
                db, conn, mapping, internal_schema=internal_schema, external_conns=external_conns,
            )
        return ValidationReport(
            count_diffs=count_diffs, sample_diffs=sample_diffs, sampled_rows=sampled_rows,
            on_missing_diffs=on_missing_diffs,
        )
    finally:
        client.close()
        close_external_connections(external_conns)
