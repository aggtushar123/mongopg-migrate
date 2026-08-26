"""Rule-based candidate mapping generation.

Implements PRD §6 step 3 ("tool generates a candidate mapping ... including
detection of likely one-to-many splits ... an inferred id_strategy per
entity ... Ambiguous or low-confidence mappings are flagged, never silently
guessed") and PRD §7's P0 "candidate field/table mapping generation".

This is deliberately name/type heuristics only — no LLM call here. The
optional LLM-assisted path (PRD §7/§8 P1) lives in `mapping/llm_propose.py`
and `mapping/llm_client.py`, and runs strictly *after* this module: it only
sees the fields this module's rule-based matching already gave up on
(`entity.unmapped.drop`/`.jsonb`), never revisits what this module was
confident about. Everything both modules emit is a *proposal*: PRD §6
step 4 requires human review before anything is written to Postgres, so
low-confidence guesses are safe to make as long as they're flagged, which
is what `ProposalIssue` is for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from mongopg_migrate.introspect.mongo import CollectionSchema
from mongopg_migrate.introspect.postgres import PostgresSchema, TableSchema
from mongopg_migrate.mapping.schema import (
    EntityMapping,
    ExplodeSpec,
    FieldSpec,
    ForeignKeyRef,
    IdStrategy,
    IdStrategyType,
    JunctionSpec,
    MappingFile,
    UnmappedPolicy,
)

HIGH_CONFIDENCE = 0.85
LOW_CONFIDENCE = 0.55


@dataclass
class ProposalIssue:
    entity: str
    field: str | None
    message: str
    confidence: float | None = None


# --- name matching -------------------------------------------------------------


def normalize_name(s: str) -> str:
    """camelCase/PascalCase -> snake_case, lowercased, for comparison."""
    s1 = re.sub(r"(?<!^)(?=[A-Z])", "_", s)
    return s1.lower().replace("-", "_")


def singular(s: str) -> str:
    if s.endswith("ies"):
        return s[:-3] + "y"
    if s.endswith("s") and not s.endswith("ss"):
        return s[:-1]
    return s


def name_similarity(a: str, b: str) -> float:
    na, nb = normalize_name(a), normalize_name(b)
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def _best_match(name: str, candidates: list[str], *, also_try_singular: bool = True) -> tuple[str | None, float]:
    best, best_score = None, 0.0
    for c in candidates:
        score = name_similarity(name, c)
        if also_try_singular:
            score = max(score, name_similarity(singular(name), singular(c)))
        if score > best_score:
            best, best_score = c, score
    return best, best_score


def match_table(collection_name: str, pg_schema: PostgresSchema) -> tuple[str | None, float]:
    return _best_match(collection_name, list(pg_schema.tables))


def match_column(field_name: str, table: TableSchema) -> tuple[str | None, float]:
    return _best_match(field_name, list(table.columns), also_try_singular=False)


def find_fk_column(table: TableSchema, target_table: str) -> str | None:
    for fk in table.foreign_keys:
        if fk.references_table == target_table:
            return fk.column
    return None


def find_child_table(parent_table: str, field_name: str, pg_schema: PostgresSchema) -> str | None:
    """Locate an existing child table for an embedded-array field, e.g.
    `orders.items` -> `order_items`. Never creates one (PRD §4 non-goal)."""
    fn = normalize_name(field_name)
    for candidate in (f"{parent_table}_{fn}", f"{singular(parent_table)}_{fn}", fn):
        if candidate in pg_schema.tables:
            return candidate
    best, best_score = None, 0.0
    for tname, t in pg_schema.tables.items():
        if find_fk_column(t, parent_table) is None:
            continue
        score = name_similarity(field_name, tname)
        if score > best_score:
            best, best_score = tname, score
    return best if best_score >= LOW_CONFIDENCE else None


def find_junction_table(parent_table: str, field_name: str, pg_schema: PostgresSchema) -> str | None:
    """Locate an existing many-to-many join table for a scalar-ID-array
    field, e.g. `posts.tagIds` -> `post_tags`. Requires >=2 FKs (one back to
    the parent) — that's what distinguishes it from a plain child table."""
    best, best_score = None, 0.0
    for tname, t in pg_schema.tables.items():
        fk_targets = {fk.references_table for fk in t.foreign_keys}
        if parent_table not in fk_targets or len(t.foreign_keys) < 2:
            continue
        score = max(name_similarity(field_name, tname), name_similarity(parent_table, tname))
        if score > best_score:
            best, best_score = tname, score
    return best if best_score >= LOW_CONFIDENCE else None


def guess_entity_for_table(table_name: str, candidate_entities: set[str]) -> str | None:
    """Best-effort guess of which Mongo collection (= entity name, in this
    proposer's convention) a Postgres table's FK target corresponds to."""
    best, best_score = None, 0.0
    for e in candidate_entities:
        score = max(name_similarity(e, table_name), name_similarity(singular(e), singular(table_name)))
        if score > best_score:
            best, best_score = e, score
    return best if best_score >= HIGH_CONFIDENCE else None


# --- id_strategy inference ------------------------------------------------------


def propose_id_strategy(
    collection: CollectionSchema, table: TableSchema
) -> tuple[IdStrategy, list[ProposalIssue]]:
    issues: list[ProposalIssue] = []
    pk_cols = table.primary_key
    if len(pk_cols) != 1:
        issues.append(
            ProposalIssue(
                entity=table.name,
                field="_id",
                message=(
                    f"target table {table.name!r} has "
                    f"{'no' if not pk_cols else 'a composite'} single-column primary key; "
                    "id_strategy must be set manually"
                ),
            )
        )
        fallback_pk = pk_cols[0] if pk_cols else "id"
        return IdStrategy(type=IdStrategyType.PASSTHROUGH, source_field="_id", target_field=fallback_pk), issues

    pk_col = pk_cols[0]
    pk_type = table.columns[pk_col].data_type.lower() if pk_col in table.columns else ""
    id_stats = collection.fields.get("_id")
    id_is_objectid = id_stats is not None and "objectid" in id_stats.bson_types

    if "uuid" in pk_type and id_is_objectid:
        return IdStrategy(type=IdStrategyType.OBJECTID_TO_UUID, source_field="_id", target_field=pk_col), issues
    if pk_type in ("integer", "bigint", "smallint") or "serial" in pk_type:
        return IdStrategy(type=IdStrategyType.INT_SEQUENCE, source_field="_id", target_field=pk_col), issues
    if pk_type in ("text", "character varying", "varchar", "bytea", "uuid"):
        return IdStrategy(type=IdStrategyType.PASSTHROUGH, source_field="_id", target_field=pk_col), issues

    issues.append(
        ProposalIssue(
            entity=table.name,
            field="_id",
            message=f"could not infer id_strategy for target PK type {pk_type!r}; "
            "defaulting to passthrough — review required",
        )
    )
    return IdStrategy(type=IdStrategyType.PASSTHROUGH, source_field="_id", target_field=pk_col), issues


# --- field proposal --------------------------------------------------------------


def propose_field(
    field_name: str, table: TableSchema, entity_names: set[str]
) -> tuple[FieldSpec | None, ProposalIssue | None]:
    col_name, score = match_column(field_name, table)
    if col_name is None or score < LOW_CONFIDENCE:
        return None, ProposalIssue(
            entity=table.name, field=field_name, message="no confident column match", confidence=score
        )

    spec = FieldSpec(target=col_name)
    issue = None
    if score < HIGH_CONFIDENCE:
        issue = ProposalIssue(
            entity=table.name,
            field=field_name,
            message=f"matched to column {col_name!r} at medium confidence — review before confirming",
            confidence=score,
        )

    if field_name.lower().endswith("id") and field_name != "_id":
        stem = normalize_name(field_name[:-2])
        for ename in entity_names:
            if name_similarity(stem, ename) >= HIGH_CONFIDENCE or name_similarity(
                stem, singular(ename)
            ) >= HIGH_CONFIDENCE:
                spec.lookup = ename
                break

    return spec, issue


# --- entity proposal --------------------------------------------------------------


def propose_entity(
    collection: CollectionSchema, pg_schema: PostgresSchema, entity_names: set[str]
) -> tuple[EntityMapping | None, list[ProposalIssue]]:
    issues: list[ProposalIssue] = []
    table_name, score = match_table(collection.name, pg_schema)
    if table_name is None or score < LOW_CONFIDENCE:
        issues.append(
            ProposalIssue(
                entity=collection.name,
                field=None,
                message="no confident target table match — mapping for this collection "
                "must be written by hand",
                confidence=score,
            )
        )
        return None, issues

    table = pg_schema.tables[table_name]
    id_strategy, id_issues = propose_id_strategy(collection, table)
    issues += id_issues

    fields: dict[str, FieldSpec] = {}
    explode: dict[str, ExplodeSpec] = {}
    junction: dict[str, JunctionSpec] = {}
    handled: set[str] = {"_id"}
    flagged: set[str] = set()

    for path in sorted(collection.top_level_field_names()):
        if path == "_id":
            continue
        stats = collection.fields[path]
        non_null_types = stats.bson_types - {"null"}

        if len(non_null_types) > 1:
            issues.append(
                ProposalIssue(
                    entity=collection.name,
                    field=path,
                    message=f"type variance across sample: {sorted(non_null_types)} — "
                    "needs an explicit cast/transform or unmapped disposition",
                )
            )
            flagged.add(path)
            continue

        if stats.is_array:
            if stats.array_item_kind == "object":
                child_table = find_child_table(table_name, path, pg_schema)
                if child_table is None:
                    issues.append(
                        ProposalIssue(
                            entity=collection.name,
                            field=path,
                            message="array of embedded objects with no obvious child table match "
                            "— needs manual `explode` config or unmapped.jsonb",
                        )
                    )
                    flagged.add(path)
                    continue
                child_ts = pg_schema.tables[child_table]
                parent_fk_col = find_fk_column(child_ts, table_name)
                if parent_fk_col is None:
                    issues.append(
                        ProposalIssue(
                            entity=collection.name,
                            field=path,
                            message=f"candidate child table {child_table!r} has no FK back to "
                            f"{table_name!r} — needs manual `explode` config",
                        )
                    )
                    flagged.add(path)
                    continue
                sub_fields: dict[str, FieldSpec] = {}
                prefix = f"{path}[]."
                for subpath in sorted(collection.fields):
                    if subpath.startswith(prefix) and "." not in subpath[len(prefix):] and "[]" not in subpath[len(prefix):]:
                        subname = subpath[len(prefix):]
                        subspec, subissue = propose_field(subname, child_ts, entity_names)
                        if subspec:
                            sub_fields[subname] = subspec
                        if subissue:
                            issues.append(subissue)
                explode[path] = ExplodeSpec(
                    target=child_table,
                    id_strategy=IdStrategy(type=IdStrategyType.SERIAL),
                    parent_fk=ForeignKeyRef(
                        target_field=parent_fk_col, references=f"{table_name}.{id_strategy.target_field}"
                    ),
                    fields=sub_fields,
                )
                handled.add(path)
            else:
                junction_table = find_junction_table(table_name, path, pg_schema)
                if junction_table is None:
                    issues.append(
                        ProposalIssue(
                            entity=collection.name,
                            field=path,
                            message="scalar-ID array with no obvious junction table match — "
                            "needs manual `junction` config or unmapped.jsonb",
                        )
                    )
                    flagged.add(path)
                    continue
                jt = pg_schema.tables[junction_table]
                parent_col = find_fk_column(jt, table_name)
                child_fk = next((fk for fk in jt.foreign_keys if fk.column != parent_col), None)
                if parent_col is None or child_fk is None:
                    issues.append(
                        ProposalIssue(
                            entity=collection.name,
                            field=path,
                            message=f"candidate junction table {junction_table!r} doesn't have the "
                            "expected two FK sides — needs manual `junction` config",
                        )
                    )
                    flagged.add(path)
                    continue
                child_entity = guess_entity_for_table(child_fk.references_table, entity_names)
                junction[path] = JunctionSpec(
                    target=junction_table,
                    parent_fk=ForeignKeyRef(
                        target_field=parent_col, references=f"{table_name}.{id_strategy.target_field}"
                    ),
                    child_fk=ForeignKeyRef(
                        target_field=child_fk.column,
                        references=f"{child_fk.references_table}.{child_fk.references_column}",
                        lookup=child_entity,
                    ),
                )
                if child_entity is None:
                    issues.append(
                        ProposalIssue(
                            entity=collection.name,
                            field=path,
                            message=f"junction child side maps to table {child_fk.references_table!r} "
                            "but no source collection could be matched for its `lookup:` — set manually",
                        )
                    )
                handled.add(path)
            continue

        if "object" in non_null_types:
            prefix = f"{path}."
            matched_any = False
            for subpath in sorted(collection.fields):
                if subpath.startswith(prefix) and "." not in subpath[len(prefix):]:
                    subname = subpath[len(prefix):]
                    spec, issue = propose_field(subname, table, entity_names)
                    if spec:
                        spec.transform = f"json_extract:{path}.{subname}"
                        fields[f"{path}.{subname}"] = spec
                        matched_any = True
                    if issue:
                        issues.append(issue)
            if matched_any:
                handled.add(path)
            else:
                flagged.add(path)
            continue

        spec, issue = propose_field(path, table, entity_names)
        if spec:
            fields[path] = spec
            handled.add(path)
        if issue:
            issues.append(issue)

    unmapped = UnmappedPolicy()
    has_jsonb_col = any("json" in c.data_type.lower() for c in table.columns.values())
    still_unmapped = collection.top_level_field_names() - handled
    for f in sorted(still_unmapped):
        target_list = unmapped.jsonb if has_jsonb_col else unmapped.drop
        target_list.append(f)
        if f not in flagged:
            issues.append(
                ProposalIssue(
                    entity=collection.name,
                    field=f,
                    message="no confident mapping found — flagged as "
                    + ("jsonb fallback" if has_jsonb_col else "drop")
                    + "; review required before confirming",
                )
            )

    entity = EntityMapping(
        source=collection.name,
        target=table_name,
        id_strategy=id_strategy,
        fields=fields,
        explode=explode,
        junction=junction,
        unmapped=unmapped,
    )
    return entity, issues


def propose_mapping(
    mongo_schemas: dict[str, CollectionSchema], pg_schema: PostgresSchema
) -> tuple[MappingFile, list[ProposalIssue]]:
    entity_names = set(mongo_schemas.keys())
    entities: dict[str, EntityMapping] = {}
    all_issues: list[ProposalIssue] = []

    for name, collection in mongo_schemas.items():
        if collection.polymorphism_candidate:
            all_issues.append(
                ProposalIssue(
                    entity=name,
                    field=collection.discriminator_field,
                    message=(
                        "shape variance detected within this collection"
                        + (
                            f" (candidate discriminator field: {collection.discriminator_field!r})"
                            if collection.discriminator_field
                            else " (no clear discriminator field found)"
                        )
                        + " — consider splitting into multiple discriminator-filtered mappings"
                        " (PRD §6 step 3); a single mapping is proposed here as a starting point only"
                    ),
                )
            )
        entity, issues = propose_entity(collection, pg_schema, entity_names)
        all_issues.extend(issues)
        if entity is not None:
            entities[name] = entity

    return MappingFile(entities=entities), all_issues
