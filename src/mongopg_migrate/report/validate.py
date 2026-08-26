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

Known, stated scope limit (no silent narrowing): the sample diff checks
each entity's own mapped fields, not its `explode`/`junction` child rows —
count diff already covers those at the row-count level; per-field sampling
of child rows is a reasonable future extension, not done here.
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
from mongopg_migrate.mapping.schema import EntityMapping, FieldSpec, MappingFile
from mongopg_migrate.migrate import idmap
from mongopg_migrate.migrate.transform import apply_default, apply_transform, get_nested

DEFAULT_SAMPLE_SIZE = 200


@dataclass
class CountDiff:
    entity: str
    table: str
    mongo_count: int
    postgres_count: int

    @property
    def matches(self) -> bool:
        return self.mongo_count == self.postgres_count


@dataclass
class SampleDiff:
    entity: str
    source_id: str
    mismatched_fields: list[str]


@dataclass
class ValidationReport:
    count_diffs: list[CountDiff] = field(default_factory=list)
    sample_diffs: list[SampleDiff] = field(default_factory=list)
    sampled_rows: int = 0

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


def _sum_array_length(db: Database, collection: str, field_name: str) -> int:
    pipeline = [
        {"$project": {"n": {"$size": {"$ifNull": [f"${field_name}", []]}}}},
        {"$group": {"_id": None, "total": {"$sum": "$n"}}},
    ]
    result = list(db[collection].aggregate(pipeline))
    return result[0]["total"] if result else 0


def _count_diffs(db: Database, conn: psycopg.Connection, mapping: MappingFile) -> list[CountDiff]:
    diffs: list[CountDiff] = []
    for name, entity in mapping.entities.items():
        diffs.append(
            CountDiff(
                entity=name,
                table=entity.target,
                mongo_count=db[entity.source].count_documents({}),
                postgres_count=_table_count(conn, entity.target),
            )
        )
        for ename, exp in entity.explode.items():
            diffs.append(
                CountDiff(
                    entity=f"{name}.{ename}",
                    table=exp.target,
                    mongo_count=_sum_array_length(db, entity.source, ename),
                    postgres_count=_table_count(conn, exp.target),
                )
            )
        for jname, junc in entity.junction.items():
            diffs.append(
                CountDiff(
                    entity=f"{name}.{jname}",
                    table=junc.target,
                    mongo_count=_sum_array_length(db, entity.source, jname),
                    postgres_count=_table_count(conn, junc.target),
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
    both are normalized to the same minimal-coefficient form first.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value)).normalize()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime.datetime):
        return value.astimezone(datetime.UTC).isoformat()
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
) -> Any:
    """Mirrors migrate/load.py's field resolution exactly (same transform,
    same lookup-via-id_map), but read-only: a lookup miss here is reported
    as a mismatch rather than raised, since by validation time the row is
    already loaded — if id_map no longer has the entry, that is itself the
    finding, not a reason to crash."""
    raw = get_nested(doc, key)
    if fspec.lookup:
        if raw is None:
            return None
        target_id_str = idmap.get(conn, fspec.lookup, str(raw), schema=internal_schema)
        if target_id_str is None:
            return _LOOKUP_MISSING
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
) -> tuple[list[SampleDiff], int]:
    diffs: list[SampleDiff] = []
    total_sampled = 0

    for name, entity in mapping.entities.items():
        if not entity.fields:
            continue
        field_keys = list(entity.fields.keys())
        pg_columns = [entity.fields[k].target for k in field_keys]
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
                )
                for k in field_keys
            ]

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
            diffs.append(SampleDiff(entity=name, source_id=source_id_str, mismatched_fields=mismatched))

    return diffs, total_sampled


def _values_equal(a: Any, b: Any) -> bool:
    if a is _LOOKUP_MISSING:
        return False
    return _canonicalize(a) == _canonicalize(b)


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

            count_diffs = _count_diffs(db, conn, mapping)
            sample_diffs, sampled_rows = _sample_diffs(
                db, conn, mapping, pg_schema, sample_size=sample_size, internal_schema=internal_schema
            )
        return ValidationReport(count_diffs=count_diffs, sample_diffs=sample_diffs, sampled_rows=sampled_rows)
    finally:
        client.close()
