"""LLM-assisted mapping suggestions (PRD §7 P1 / §8).

Off by default (PRD §8: "pluggable... off by default"). Only called for
fields the rule-based proposer in mapping/propose.py already gave up on —
i.e. fields sitting in an entity's `unmapped.drop`/`unmapped.jsonb` after
`propose_mapping()` runs. Rule-based matching stays authoritative for
everything it's confident about (including medium-confidence matches it
already accepted with a flagged warning — those aren't revisited here);
the LLM is asked about exactly the cases PRD §7 names it for: "ambiguous
fields, renames, and splits" — concretely, the `users.name` -> `display_name`
case that live testing showed name-similarity alone can't solve (§7's own
review history: confidence 0.50, below the auto-map threshold).

Privacy (PRD §8): `build_llm_payload` sends only field names, BSON/Postgres
types, and array shape — never a document or row value. This is enforced
by construction, not by a filter: the introspection dataclasses this reads
from (`CollectionSchema`/`FieldStats`, `TableSchema`/`ColumnInfo`) never
store sample values in the first place — there is nothing row-shaped to
accidentally leak.

Every suggestion is still just a suggestion: PRD §6 step 4's rule
("nothing is written to Postgres until the mapping is confirmed") applies
identically whether a field's mapping came from name-similarity or from
the LLM. `apply_suggestions` moves a field from `unmapped` into `fields`
when it accepts a suggestion, but the mapping file this produces is not
auto-confirmed either way — and it never trusts a suggested column blindly:
a hallucinated or already-claimed column name is rejected and reported,
never silently written.

Documented scope limit, not silent: a "split" suggestion (one Mongo field's
value naturally becoming two-or-more Postgres columns, e.g. a combined
`fullName` field against separate `first_name`/`last_name` columns) is
*surfaced* as an issue with the LLM's hint, not applied — the mapping
DSL's `fields: dict[str, FieldSpec]` is one-source-field-to-one-column by
construction (PRD §12), and representing a real split needs a DSL
extension this feature does not make.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from mongopg_migrate.introspect.mongo import CollectionSchema
from mongopg_migrate.introspect.postgres import PostgresSchema, TableSchema
from mongopg_migrate.mapping.llm_client import LLMClient, LLMClientError
from mongopg_migrate.mapping.propose import ProposalIssue
from mongopg_migrate.mapping.schema import EntityMapping, FieldSpec, MappingFile

SYSTEM_PROMPT = """You map MongoDB document fields onto PostgreSQL columns for a database migration tool.

You will be given, for one Mongo collection being migrated onto one Postgres table:
- fields already confidently mapped (for naming-convention context)
- fields that a name-similarity matcher could NOT confidently map
- the Postgres columns still available (not yet claimed by another field)

For each unresolved field, decide one action:
- "map": it corresponds to exactly one available column — a rename, an abbreviation, a different
  naming convention, etc. Set target_column to one of the EXACT candidate column names given —
  never invent a column name that isn't in the candidate list.
- "split": the field's value naturally decomposes into more than one column (e.g. a combined
  "fullName" field where the target has separate first_name/last_name columns). Do not set
  target_column; describe the split in split_hint instead.
- "none": no candidate column is a good match, or you're not confident. This is the safe
  default — an incorrect "map" wastes more of the reviewing human's time than an honest "none".

You are given only field names, types, and shapes here — never real document data. Be
conservative: every suggestion is shown to a human for review, never applied automatically."""


class FieldSuggestion(BaseModel):
    source_field: str
    action: Literal["map", "split", "none"]
    target_column: str | None = None
    split_hint: str | None = None
    reasoning: str


class SuggestionBatch(BaseModel):
    suggestions: list[FieldSuggestion]


def build_llm_payload(
    source_collection: str,
    target_table: str,
    collection: CollectionSchema,
    table: TableSchema,
    unresolved_fields: list[str],
    already_mapped: dict[str, str],
    *,
    id_column: str | None = None,
) -> dict:
    """Schema metadata only — see module docstring. `already_mapped` gives
    the LLM naming-convention context (seeing `email -> email` helps it
    infer `name -> display_name` is a rename, not a coincidence).
    `id_column` (the entity's `id_strategy.target_field`) is excluded from
    `candidate_columns` — it's populated by id_strategy, not by a `fields`
    mapping, and was never in `already_mapped` to begin with, so it would
    otherwise wrongly look available for another field to claim."""
    used_columns = set(already_mapped.values())
    if id_column:
        used_columns.add(id_column)
    candidate_columns = [
        {"name": c.name, "data_type": c.data_type, "is_nullable": c.is_nullable}
        for c in table.columns.values()
        if c.name not in used_columns
    ]
    unresolved = []
    for f in unresolved_fields:
        stats = collection.fields.get(f)
        unresolved.append(
            {
                "name": f,
                "bson_types": sorted(stats.bson_types) if stats else [],
                "is_array": stats.is_array if stats else False,
                "array_item_kind": stats.array_item_kind if stats else None,
            }
        )
    return {
        "mongo_collection": source_collection,
        "postgres_table": target_table,
        "already_mapped_fields": [
            {"source_field": k, "target_column": v} for k, v in sorted(already_mapped.items())
        ],
        "unresolved_fields": unresolved,
        "candidate_columns": candidate_columns,
    }


def suggest_for_entity(
    client: LLMClient,
    collection: CollectionSchema,
    table: TableSchema,
    entity: EntityMapping,
) -> SuggestionBatch | None:
    unresolved = sorted(set(entity.unmapped.drop) | set(entity.unmapped.jsonb))
    if not unresolved:
        return None
    already_mapped = {k: v.target for k, v in entity.fields.items()}
    payload = build_llm_payload(
        entity.source,
        entity.target,
        collection,
        table,
        unresolved,
        already_mapped,
        id_column=entity.id_strategy.target_field,
    )
    return client.suggest(system=SYSTEM_PROMPT, user_payload=payload, output_schema=SuggestionBatch)


def apply_suggestions(
    entity_name: str, entity: EntityMapping, table: TableSchema, batch: SuggestionBatch
) -> list[ProposalIssue]:
    """Merges validated suggestions into `entity` in place. Never trusts
    the LLM's `target_column` blindly: rejected (and reported, not
    silently dropped) if it doesn't name a real column on the table, or if
    another field already claims it — an LLM can hallucinate a plausible-
    looking but wrong or duplicate column name same as any other text
    generation."""
    issues: list[ProposalIssue] = []
    # The id column is claimed by id_strategy, not by a `fields` entry — never a valid
    # suggestion target even if a non-compliant response ignores the candidate list.
    used_columns = {fs.target for fs in entity.fields.values()} | {entity.id_strategy.target_field}

    for s in batch.suggestions:
        if s.source_field not in entity.unmapped.drop and s.source_field not in entity.unmapped.jsonb:
            continue  # stale/unexpected field name in the response — ignore rather than trust blindly

        if s.action == "map":
            if not s.target_column or s.target_column not in table.columns:
                issues.append(
                    ProposalIssue(
                        entity=entity_name,
                        field=s.source_field,
                        message=f"LLM suggested mapping to {s.target_column!r}, which is not a real "
                        f"column on {table.name!r} — ignored. ({s.reasoning})",
                    )
                )
                continue
            if s.target_column in used_columns:
                issues.append(
                    ProposalIssue(
                        entity=entity_name,
                        field=s.source_field,
                        message=f"LLM suggested mapping to {s.target_column!r}, but that column is "
                        f"already claimed by another field — ignored. ({s.reasoning})",
                    )
                )
                continue
            entity.fields[s.source_field] = FieldSpec(target=s.target_column)
            entity.unmapped.drop = [f for f in entity.unmapped.drop if f != s.source_field]
            entity.unmapped.jsonb = [f for f in entity.unmapped.jsonb if f != s.source_field]
            used_columns.add(s.target_column)
            issues.append(
                ProposalIssue(
                    entity=entity_name,
                    field=s.source_field,
                    message=f"LLM-suggested mapping to {s.target_column!r} — still requires human "
                    f"confirmation (PRD §6 step 4). Reasoning: {s.reasoning}",
                )
            )
        elif s.action == "split":
            issues.append(
                ProposalIssue(
                    entity=entity_name,
                    field=s.source_field,
                    message=f"LLM suggests this field should split across multiple columns "
                    f"({s.split_hint}) — the mapping format doesn't support one field -> many "
                    f"columns yet; wire this up by hand if it's right. Reasoning: {s.reasoning}",
                )
            )
        else:  # "none"
            issues.append(
                ProposalIssue(
                    entity=entity_name,
                    field=s.source_field,
                    message=f"LLM found no confident mapping either. Reasoning: {s.reasoning}",
                )
            )
    return issues


def enrich_mapping_with_llm(
    client: LLMClient,
    mapping: MappingFile,
    mongo_schemas: dict[str, CollectionSchema],
    pg_schema: PostgresSchema,
) -> list[ProposalIssue]:
    """Runs suggest_for_entity + apply_suggestions across every entity with
    unresolved fields. An LLMClientError from one entity is caught and
    reported as an issue rather than aborting the run — a transient API
    failure on one entity shouldn't discard the rule-based mapping already
    produced for the rest."""
    all_issues: list[ProposalIssue] = []
    for entity_name, entity in mapping.entities.items():
        collection = mongo_schemas.get(entity.source)
        table = pg_schema.tables.get(entity.target)
        if collection is None or table is None:
            continue
        try:
            batch = suggest_for_entity(client, collection, table, entity)
        except LLMClientError as e:
            all_issues.append(ProposalIssue(entity=entity_name, field=None, message=f"LLM assist failed: {e}"))
            continue
        if batch is None:
            continue
        all_issues.extend(apply_suggestions(entity_name, entity, table, batch))
    return all_issues
