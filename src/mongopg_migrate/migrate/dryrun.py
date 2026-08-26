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
    ExplodeSpec,
    FieldSpec,
    MappingFile,
    UnpivotSpec,
)
from mongopg_migrate.migrate import idmap
from mongopg_migrate.migrate.load import (
    LoadError,
    close_external_connections,
    open_external_connections,
)
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


def _flatten_explode(explode: dict[str, ExplodeSpec], *, path_prefix: str = "") -> list[tuple[str, ExplodeSpec]]:
    # Mirrors migrate/load.py's private helper of the same name (parent-
    # before-child pre-order) — kept as a separate small copy rather than
    # importing a leading-underscore name across modules.
    out: list[tuple[str, ExplodeSpec]] = []
    for ename, exp in explode.items():
        path = f"{path_prefix}.{ename}" if path_prefix else ename
        out.append((path, exp))
        out.extend(_flatten_explode(exp.explode, path_prefix=path))
    return out


def _mapped_tables(mapping: MappingFile) -> set[str]:
    tables: set[str] = set()
    for entity in mapping.entities.values():
        tables.add(entity.target)
        tables.update(exp.target for _, exp in _flatten_explode(entity.explode))
        tables.update(junc.target for junc in entity.junction.values())
        tables.update(unp.target for unp in entity.unpivot.values())
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


def _gather_explode_lookup_needs(item: dict, exp: ExplodeSpec) -> dict[str, set]:
    """Recurses into nested `explode` children so a `lookup:` on a
    grandchild-level field (e.g. `categoryParts[].departmentId`) is
    collected too, not just the top explode level's own fields."""
    needs = _gather_field_lookup_needs(item, exp.fields)
    for nested_ename, nested_exp in exp.explode.items():
        for nested_item in _as_array(item.get(nested_ename)):
            _merge_needs(needs, _gather_explode_lookup_needs(nested_item, nested_exp))
    return needs


def _as_array(value: object) -> list:
    """Permissive coercion for a field that's *supposed* to be an array —
    silently returns `[]` for anything else (including a string or dict,
    both of which are iterable in Python but wrong here — see
    `_validate_array_shapes`, which is what actually reports this as a
    violation; this helper just avoids the needs-collection pass from also
    iterating a string character-by-character while that violation is
    surfacing elsewhere)."""
    return value if isinstance(value, list) else []


def _validate_explode_array_shapes(item: dict, exp: ExplodeSpec, *, entity_name: str, field_path: str) -> list[DryRunViolation]:
    """Recurses into nested `explode` children so a scalar-where-array-
    expected mistake at a grandchild level (e.g. `categoryParts` present
    but not actually a list) is caught too, not just at the top level."""
    violations: list[DryRunViolation] = []
    for nested_ename, nested_exp in exp.explode.items():
        nested_path = f"{field_path}.{nested_ename}"
        value = item.get(nested_ename)
        if value is not None and not isinstance(value, list):
            violations.append(
                DryRunViolation(
                    entity=entity_name,
                    layer="fast",
                    field=nested_path,
                    message=f"explode field {nested_path!r} is not an array (got {type(value).__name__}: "
                    f"{value!r}) — this would silently iterate wrong at migrate time (a string iterates "
                    "character-by-character); fix the mapping or the source data",
                )
            )
            continue
        for nested_item in _as_array(value):
            violations.extend(
                _validate_explode_array_shapes(nested_item, nested_exp, entity_name=entity_name, field_path=nested_path)
            )
    return violations


def _validate_array_shapes(doc: dict, entity: EntityMapping, entity_name: str) -> list[DryRunViolation]:
    """`explode`/`junction` fields must be a real Mongo array. Catches the
    same shape problem migrate/load.py's `_require_array` refuses to run
    on — a scalar string is technically iterable in Python (character by
    character) but not what either construct means; a scalar reference
    belongs in `fields:` with `lookup:` instead of `junction:`."""
    violations: list[DryRunViolation] = []
    for ename, exp in entity.explode.items():
        value = doc.get(ename)
        if value is not None and not isinstance(value, list):
            violations.append(
                DryRunViolation(
                    entity=entity_name,
                    layer="fast",
                    field=ename,
                    message=f"explode field {ename!r} is not an array (got {type(value).__name__}: "
                    f"{value!r}) — this would silently iterate wrong at migrate time (a string iterates "
                    "character-by-character); fix the mapping or the source data",
                )
            )
            continue
        for item in _as_array(value):
            violations.extend(_validate_explode_array_shapes(item, exp, entity_name=entity_name, field_path=ename))
    for jname in entity.junction:
        value = doc.get(jname)
        if value is not None and not isinstance(value, list):
            violations.append(
                DryRunViolation(
                    entity=entity_name,
                    layer="fast",
                    field=jname,
                    message=f"junction field {jname!r} is not an array (got {type(value).__name__}: "
                    f"{value!r}) — a single scalar reference belongs in `fields:` with `lookup:` instead "
                    "of `junction:`",
                )
            )
    return violations


def _collect_batch_lookup_needs(batch: list[dict], entity: EntityMapping) -> dict[str, set]:
    needs: dict[str, set] = {}
    for doc in batch:
        _merge_needs(needs, _gather_field_lookup_needs(doc, entity.fields))
        for ename, exp in entity.explode.items():
            for item in _as_array(doc.get(ename)):
                _merge_needs(needs, _gather_explode_lookup_needs(item, exp))
        for jname, junc in entity.junction.items():
            if junc.child_fk.lookup:
                for child_source in _as_array(doc.get(jname)):
                    if child_source is not None:
                        needs.setdefault(junc.child_fk.lookup, set()).add(child_source)
    return needs


def _check_existence(
    db: Database,
    mapping: MappingFile,
    needs: dict[str, set],
    *,
    conn: psycopg.Connection | None = None,
    internal_schema: str = idmap.DEFAULT_SCHEMA_NAME,
    external_conns: dict[str, psycopg.Connection] | None = None,
) -> dict[str, set[str]]:
    """One batched `$in` existence query per referenced entity, against
    Mongo directly (see module docstring for why not against id_map, in
    general). The one exception is a `lookup:` naming a declared
    `external_entities` entry (PRD §12's cross-run case: `users` was
    migrated in an earlier run, this mapping only covers `orders`) — there
    is no local Mongo-side collection mapping to check against locally in
    the way a declared entity has, so those are checked against the real
    `_mongopg.id_map` instead: on `external_conns[lookup_entity]` when the
    entity lives in a *different* database (`mapping.external_databases`),
    else on this run's own `conn`. Without any connection available,
    external lookups are left unverified here — dry-run's Layer A doesn't
    hard-require Postgres access, so this degrades gracefully rather than
    failing; `validate_structure` already ensures every non-external
    `lookup:` target is a real, declared entity before dry-run even runs.
    """
    found: dict[str, set[str]] = {}
    for lookup_entity, raw_values in needs.items():
        if not raw_values:
            continue
        target_entity = mapping.entities.get(lookup_entity)
        if target_entity is not None:
            coll = db[target_entity.source]
            docs = coll.find({"_id": {"$in": list(raw_values)}}, {"_id": 1})
            found[lookup_entity] = {str(d["_id"]) for d in docs}
            continue
        if lookup_entity not in mapping.external_entities:
            continue
        # Same rule as migrate/load.py's _resolve_lookup: a cross-database
        # external entity always uses its own database's real, default
        # schema — never this run's own (possibly Layer-B-disposable)
        # internal_schema.
        if lookup_entity in (external_conns or {}):
            id_map_conn = external_conns[lookup_entity]
            id_map_schema = idmap.DEFAULT_SCHEMA_NAME
        else:
            id_map_conn = conn
            id_map_schema = internal_schema
        if id_map_conn is None:
            continue
        found[lookup_entity] = {
            str(raw)
            for raw in raw_values
            if idmap.get(id_map_conn, lookup_entity, str(raw), schema=id_map_schema) is not None
        }
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


def _validate_explode_item(
    item: dict,
    exp: ExplodeSpec,
    *,
    context: str,
    pg_schema: PostgresSchema,
    found: dict[str, set[str]],
) -> list[DryRunViolation]:
    """Validates one exploded item's own fields via `_validate_field`, then
    recurses into any nested `explode` children — a grandchild-level
    lookup miss, transform error, or NOT NULL violation is caught here too,
    not just at the top explode level."""
    violations: list[DryRunViolation] = []
    for key, fspec in exp.fields.items():
        violations.extend(
            _validate_field(item, key, fspec, context=context, target_table=exp.target, pg_schema=pg_schema, found=found)
        )
    for nested_ename, nested_exp in exp.explode.items():
        nested_context = f"{context}.{nested_ename}"
        for nested_item in _as_array(item.get(nested_ename)):
            violations.extend(
                _validate_explode_item(nested_item, nested_exp, context=nested_context, pg_schema=pg_schema, found=found)
            )
    return violations


def _validate_unpivot_items(
    doc: dict, uname: str, unp: UnpivotSpec, *, context: str, pg_schema: PostgresSchema
) -> list[DryRunViolation]:
    violations: list[DryRunViolation] = []
    for item in unp.items:
        raw = get_nested(doc, item.source_field)
        if raw is None and unp.skip_null:
            continue
        try:
            value = apply_transform(item.transform, raw)
        except TransformError as e:
            violations.append(
                DryRunViolation(entity=context, layer="fast", field=f"{uname}.{item.source_field}", message=str(e))
            )
            continue
        value = apply_default(item.transform, value)
        if value is None:
            table_schema = pg_schema.tables.get(unp.target)
            col_info = table_schema.columns.get(unp.value_column) if table_schema else None
            if col_info is None:
                violations.append(
                    DryRunViolation(
                        entity=context,
                        layer="fast",
                        field=f"{uname}.{item.source_field}",
                        message=f"target column {unp.target}.{unp.value_column} not found in the "
                        "introspected Postgres schema",
                    )
                )
            elif not col_info.is_nullable:
                violations.append(
                    DryRunViolation(
                        entity=context,
                        layer="fast",
                        field=f"{uname}.{item.source_field}",
                        message=f"null value for NOT NULL column {unp.target}.{unp.value_column} "
                        f"(source field {item.source_field!r} missing/null and no `default:` transform set)",
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
    postgres_dsn: str | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    sample_size: int | None = None,
    max_violations: int = DEFAULT_MAX_VIOLATIONS,
) -> DryRunReport:
    """`postgres_dsn` is optional and read-only when given: it's only used
    to check `_mongopg.id_map` for `lookup:`s that name an `external_entities`
    entry (see `_check_existence`) that lives in *this* database. An entity
    listed in `mapping.external_databases` (a different database entirely —
    the microservices-split case) is checked on its own connection
    regardless of whether `postgres_dsn` is given. Without `postgres_dsn`,
    same-database external lookups simply aren't checked — everything else
    about Layer A is unaffected."""
    order = mapping.entity_load_order()  # also surfaces CircularEntityDependencyError early

    client: MongoClient = MongoClient(mongo_uri)
    violations: list[DryRunViolation] = []
    truncated = False
    pg_conn = psycopg.connect(postgres_dsn) if postgres_dsn else None
    external_conns = open_external_connections(mapping)
    try:
        db: Database = client.get_default_database()
        if db is None:
            raise ValueError("MONGO_URI must include a default database")

        for entity_name in order:
            entity = mapping.entities[entity_name]
            cursor = db[entity.source].find(entity.mongo_filter()).sort("_id", 1)
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
                found = _check_existence(db, mapping, needs, conn=pg_conn, external_conns=external_conns)

                for doc in batch:
                    violations.extend(_validate_id_strategy(doc, entity, entity_name))
                    violations.extend(_validate_array_shapes(doc, entity, entity_name))

                    for key, fspec in entity.fields.items():
                        violations.extend(
                            _validate_field(
                                doc, key, fspec, context=entity_name, target_table=entity.target,
                                pg_schema=pg_schema, found=found,
                            )
                        )

                    for ename, exp in entity.explode.items():
                        for item in _as_array(doc.get(ename)):
                            violations.extend(
                                _validate_explode_item(
                                    item, exp, context=f"{entity_name}.{ename}", pg_schema=pg_schema, found=found,
                                )
                            )

                    for jname, junc in entity.junction.items():
                        if not junc.child_fk.lookup:
                            continue
                        for child_source in _as_array(doc.get(jname)):
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

                    for uname, unp in entity.unpivot.items():
                        violations.extend(
                            _validate_unpivot_items(doc, uname, unp, context=entity_name, pg_schema=pg_schema)
                        )
            if truncated:
                break
    finally:
        client.close()
        if pg_conn is not None:
            pg_conn.close()
        close_external_connections(external_conns)

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
    report = run_fast_pass(
        mapping, mongo_uri, pg_schema, postgres_dsn=postgres_dsn, batch_size=batch_size, sample_size=sample_size
    )
    if report.ok or force_realistic:
        realistic = run_realistic_pass(mapping, mongo_uri, postgres_dsn, pg_schema, batch_size=batch_size)
        report.violations.extend(realistic.violations)
    return report
