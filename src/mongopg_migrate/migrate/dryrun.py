"""Dry-run: two layers, neither of which commits to the real target tables.

PRD §6 step 5 / §7 / §8.

Layer A — fast pass (no Postgres write at all; Postgres is only *read*,
via the already-introspected `PostgresSchema` the caller passes in):
    Walks every entity's *full* Mongo collection (not just introspection's
    sample — a dry run's whole point is catching what a sample could miss)
    in `MappingFile.entity_load_order()`, applying each field's `transform`
    + NOT NULL check exactly as migrate/load.py would, and checks every
    `lookup:` for existence. Since this typically runs *before* any real
    load has happened, `_mongopg.id_map` may be empty or stale — so lookups
    are checked against the referenced Mongo collection directly (batched
    `$in` existence queries, mirroring migrate/load.py's batching), not
    against id_map. This deliberately trades a little accuracy (it can't
    catch "the referenced entity's own id_strategy would fail" — Layer B
    catches that) for being usable at any time, not just after a real load.

Layer B — realistic pass (optional, recommended before a real migration):
    1. Clone every mapped table's structure (`LIKE ... INCLUDING ALL`) into
       a disposable schema, then add FK constraints among the clones only
       (PRD §10 "Temp-schema FKs": never a cross-schema FK back to the real
       target schema).
    2. Run the *real* loader (migrate/load.py) against that clone, using a
       throwaway `_mongopg_dryrun_<...>` schema for id_map/checkpoint so
       nothing here can make a later real `migrate` think work is already
       done that never actually happened.
    3. Drop both disposable schemas, success or failure.
    This catches what Layer A structurally cannot: real constraint
    violations, real FK-order behavior, real column-type coercion.
    Known, accepted limitation: a `SERIAL`/`IDENTITY` column's `LIKE`-cloned
    default still points at the *original* table's sequence, so a realistic
    pass consumes a few real sequence values. Harmless — Postgres sequences
    are explicitly allowed to have gaps — but worth knowing about.

Both layers' violations land in one `DryRunReport` (PRD §6 step 5: "Report
combines both").
"""

from __future__ import annotations

import itertools
import uuid
from dataclasses import dataclass, field

import psycopg
from pymongo import MongoClient
from pymongo.database import Database

from mongopg_migrate.introspect.postgres import PostgresSchema
from mongopg_migrate.mapping.schema import (
    CircularEntityDependencyError,
    EntityMapping,
    FieldSpec,
    MappingFile,
)
from mongopg_migrate.migrate.load import LoadError
from mongopg_migrate.migrate.load import load as run_batch_load
from mongopg_migrate.migrate.transform import (
    TransformError,
    apply_default,
    apply_transform,
    get_nested,
)

DEFAULT_BATCH_SIZE = 500
DEFAULT_MAX_VIOLATIONS = 200


@dataclass
class DryRunViolation:
    entity: str
    layer: str  # "fast" | "realistic"
    field: str | None
    message: str


@dataclass
class DryRunReport:
    violations: list[DryRunViolation] = field(default_factory=list)
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return not self.violations


# --- Layer A: fast pass -------------------------------------------------------


def _mapped_tables(mapping: MappingFile) -> set[str]:
    # Mirrors migrate/load.py's private helper of the same name — kept as a
    # separate small copy rather than importing a leading-underscore name
    # across modules.
    tables: set[str] = set()
    for entity in mapping.entities.values():
        tables.add(entity.target)
        tables.update(exp.target for exp in entity.explode.values())
        tables.update(junc.target for junc in entity.junction.values())
    return tables


def _gather_field_lookup_needs(record: dict, fields: dict[str, FieldSpec]) -> dict[str, set]:
    needs: dict[str, set] = {}
    for key, fspec in fields.items():
        if not fspec.lookup:
            continue
        raw = get_nested(record, key)
        if raw is not None:
            needs.setdefault(fspec.lookup, set()).add(raw)
    return needs


def _merge_needs(into: dict[str, set], other: dict[str, set]) -> None:
    for k, v in other.items():
        into.setdefault(k, set()).update(v)


def _collect_batch_lookup_needs(batch: list[dict], entity: EntityMapping) -> dict[str, set]:
    needs: dict[str, set] = {}
    for doc in batch:
        _merge_needs(needs, _gather_field_lookup_needs(doc, entity.fields))
        for ename, exp in entity.explode.items():
            for item in doc.get(ename) or []:
                _merge_needs(needs, _gather_field_lookup_needs(item, exp.fields))
        for jname, junc in entity.junction.items():
            if junc.child_fk.lookup:
                for child_source in doc.get(jname) or []:
                    if child_source is not None:
                        needs.setdefault(junc.child_fk.lookup, set()).add(child_source)
    return needs


def _check_existence(db: Database, mapping: MappingFile, needs: dict[str, set]) -> dict[str, set[str]]:
    """One batched `$in` existence query per referenced entity, against
    Mongo directly — see module docstring for why not against id_map."""
    found: dict[str, set[str]] = {}
    for lookup_entity, raw_values in needs.items():
        target_entity = mapping.entities.get(lookup_entity)
        if target_entity is None or not raw_values:
            continue
        coll = db[target_entity.source]
        docs = coll.find({"_id": {"$in": list(raw_values)}}, {"_id": 1})
        found[lookup_entity] = {str(d["_id"]) for d in docs}
    return found


def _validate_field(
    record: dict,
    key: str,
    fspec: FieldSpec,
    *,
    context: str,
    target_table: str,
    pg_schema: PostgresSchema,
    found: dict[str, set[str]],
) -> list[DryRunViolation]:
    violations: list[DryRunViolation] = []
    raw = get_nested(record, key)

    if fspec.lookup:
        if raw is not None and str(raw) not in found.get(fspec.lookup, set()):
            violations.append(
                DryRunViolation(
                    entity=context,
                    layer="fast",
                    field=key,
                    message=f"lookup miss: {fspec.lookup}/{raw!r} not found in the {fspec.lookup!r} "
                    "source collection — this would fail at migrate time",
                )
            )
        value = raw
    else:
        try:
            value = apply_transform(fspec.transform, raw)
        except TransformError as e:
            violations.append(DryRunViolation(entity=context, layer="fast", field=key, message=str(e)))
            return violations

    value = apply_default(fspec.transform, value)
    if value is None:
        table_schema = pg_schema.tables.get(target_table)
        col_info = table_schema.columns.get(fspec.target) if table_schema else None
        if col_info is None:
            violations.append(
                DryRunViolation(
                    entity=context,
                    layer="fast",
                    field=key,
                    message=f"target column {target_table}.{fspec.target} not found in the introspected "
                    "Postgres schema",
                )
            )
        elif not col_info.is_nullable:
            violations.append(
                DryRunViolation(
                    entity=context,
                    layer="fast",
                    field=key,
                    message=f"null value for NOT NULL column {target_table}.{fspec.target} "
                    "(source field missing/null and no `default:` transform set)",
                )
            )
    return violations


def _validate_id_strategy(doc: dict, entity: EntityMapping, entity_name: str) -> list[DryRunViolation]:
    from bson.objectid import ObjectId

    strategy = entity.id_strategy
    source_field = strategy.source_field or "_id"
    raw = get_nested(doc, source_field)
    if raw is None:
        return [
            DryRunViolation(
                entity=entity_name,
                layer="fast",
                field=source_field,
                message=f"id_strategy.source_field {source_field!r} is missing on this document — "
                "cannot establish identity",
            )
        ]
    if strategy.type.value == "objectid_to_uuid" and not isinstance(raw, ObjectId):
        return [
            DryRunViolation(
                entity=entity_name,
                layer="fast",
                field=source_field,
                message=f"id_strategy objectid_to_uuid expects an ObjectId, got {type(raw).__name__}",
            )
        ]
    return []


def run_fast_pass(
    mapping: MappingFile,
    mongo_uri: str,
    pg_schema: PostgresSchema,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    sample_size: int | None = None,
    max_violations: int = DEFAULT_MAX_VIOLATIONS,
) -> DryRunReport:
    order = mapping.entity_load_order()  # also surfaces CircularEntityDependencyError early

    client: MongoClient = MongoClient(mongo_uri)
    violations: list[DryRunViolation] = []
    truncated = False
    try:
        db: Database = client.get_default_database()
        if db is None:
            raise ValueError("MONGO_URI must include a default database")

        for entity_name in order:
            entity = mapping.entities[entity_name]
            cursor = db[entity.source].find().sort("_id", 1)
            if sample_size is not None:
                cursor = cursor.limit(sample_size)

            while True:
                if len(violations) >= max_violations:
                    truncated = True
                    break
                batch = list(itertools.islice(cursor, batch_size))
                if not batch:
                    break

                needs = _collect_batch_lookup_needs(batch, entity)
                found = _check_existence(db, mapping, needs)

                for doc in batch:
                    violations.extend(_validate_id_strategy(doc, entity, entity_name))

                    for key, fspec in entity.fields.items():
                        violations.extend(
                            _validate_field(
                                doc, key, fspec, context=entity_name, target_table=entity.target,
                                pg_schema=pg_schema, found=found,
                            )
                        )

                    for ename, exp in entity.explode.items():
                        for item in doc.get(ename) or []:
                            for key, fspec in exp.fields.items():
                                violations.extend(
                                    _validate_field(
                                        item, key, fspec, context=f"{entity_name}.{ename}",
                                        target_table=exp.target, pg_schema=pg_schema, found=found,
                                    )
                                )

                    for jname, junc in entity.junction.items():
                        if not junc.child_fk.lookup:
                            continue
                        for child_source in doc.get(jname) or []:
                            if child_source is not None and str(child_source) not in found.get(
                                junc.child_fk.lookup, set()
                            ):
                                violations.append(
                                    DryRunViolation(
                                        entity=entity_name,
                                        layer="fast",
                                        field=jname,
                                        message=f"junction lookup miss: {junc.child_fk.lookup}/"
                                        f"{child_source!r} not found — this would fail at migrate time",
                                    )
                                )
            if truncated:
                break
    finally:
        client.close()

    return DryRunReport(violations=violations, truncated=truncated)


# --- Layer B: realistic pass ---------------------------------------------------


def _clone_schema_for_dryrun(
    conn: psycopg.Connection, mapping: MappingFile, pg_schema: PostgresSchema, *, source_schema: str = "public"
) -> str:
    temp_schema = f"migrate_dryrun_{uuid.uuid4().hex[:10]}"
    tables = sorted(_mapped_tables(mapping))
    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{temp_schema}"')
        for t in tables:
            cur.execute(f'CREATE TABLE "{temp_schema}"."{t}" (LIKE "{source_schema}"."{t}" INCLUDING ALL)')
        # Second pass: FKs among the clones only (PRD §10 "Temp-schema FKs" —
        # never a cross-schema FK back to the real target schema). Tables must
        # all exist first, hence two passes.
        for t in tables:
            for fk in pg_schema.tables[t].foreign_keys:
                if fk.references_table not in tables:
                    continue
                deferrable = " DEFERRABLE INITIALLY DEFERRED" if fk.is_deferrable else ""
                cur.execute(
                    f'ALTER TABLE "{temp_schema}"."{t}" ADD FOREIGN KEY ("{fk.column}") '
                    f'REFERENCES "{temp_schema}"."{fk.references_table}" ("{fk.references_column}"){deferrable}'
                )
    return temp_schema


def _drop_dryrun_artifacts(conn: psycopg.Connection, temp_schema: str, internal_schema: str) -> None:
    with conn.cursor() as cur:
        cur.execute(f'DROP SCHEMA IF EXISTS "{temp_schema}" CASCADE')
        cur.execute(f'DROP SCHEMA IF EXISTS "{internal_schema}" CASCADE')


def run_realistic_pass(
    mapping: MappingFile,
    mongo_uri: str,
    postgres_dsn: str,
    pg_schema: PostgresSchema,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> DryRunReport:
    internal_schema = f"_mongopg_dryrun_{uuid.uuid4().hex[:10]}"
    violations: list[DryRunViolation] = []

    with psycopg.connect(postgres_dsn) as setup_conn:
        setup_conn.autocommit = True
        try:
            temp_schema = _clone_schema_for_dryrun(setup_conn, mapping, pg_schema)
        except psycopg.Error as e:
            return DryRunReport(
                violations=[DryRunViolation(entity="<schema clone>", layer="realistic", field=None, message=str(e))]
            )

        try:
            search_path = f'"{temp_schema}", public'
            run_batch_load(
                mapping,
                mongo_uri,
                postgres_dsn,
                pg_schema,
                mode="truncate",
                batch_size=batch_size,
                internal_schema=internal_schema,
                search_path=search_path,
            )
        except (LoadError, CircularEntityDependencyError) as e:
            violations.append(DryRunViolation(entity="<realistic load>", layer="realistic", field=None, message=str(e)))
        except psycopg.Error as e:
            violations.append(
                DryRunViolation(entity="<realistic load>", layer="realistic", field=None, message=f"Postgres error: {e}")
            )
        finally:
            _drop_dryrun_artifacts(setup_conn, temp_schema, internal_schema)

    return DryRunReport(violations=violations)


# --- combined entry point -------------------------------------------------------


def run(
    mapping: MappingFile,
    mongo_uri: str,
    postgres_dsn: str,
    pg_schema: PostgresSchema,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    sample_size: int | None = None,
    force_realistic: bool = False,
) -> DryRunReport:
    """Runs Layer A always; runs Layer B only if Layer A found nothing (or
    `force_realistic=True`) — no point paying for a real COPY+FK pass
    against a mapping that's already known to fail. PRD §6 step 5: "Report
    combines both."""
    report = run_fast_pass(mapping, mongo_uri, pg_schema, batch_size=batch_size, sample_size=sample_size)
    if report.ok or force_realistic:
        realistic = run_realistic_pass(mapping, mongo_uri, postgres_dsn, pg_schema, batch_size=batch_size)
        report.violations.extend(realistic.violations)
    return report
