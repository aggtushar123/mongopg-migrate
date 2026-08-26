"""Unit tests for the LLM-assist path, against a fake LLMClient — no real
API calls (no Anthropic credentials are available in CI or this dev
environment). The fake still exercises the full contract: build_llm_payload
-> client.suggest -> apply_suggestions, with the Pydantic SuggestionBatch
schema in between, exactly as AnthropicLLMClient's response.parsed_output
would be structured.
"""

import json

import pytest
from pydantic import BaseModel

from mongopg_migrate.introspect.mongo import CollectionSchema, FieldStats
from mongopg_migrate.introspect.postgres import ColumnInfo, TableSchema
from mongopg_migrate.mapping.llm_client import LLMClientError
from mongopg_migrate.mapping.llm_propose import (
    FieldSuggestion,
    SuggestionBatch,
    apply_suggestions,
    build_llm_payload,
    enrich_mapping_with_llm,
    suggest_for_entity,
)
from mongopg_migrate.mapping.schema import (
    EntityMapping,
    FieldSpec,
    IdStrategy,
    IdStrategyType,
    MappingFile,
    UnmappedPolicy,
)


class _FakeLLMClient:
    """Returns a scripted SuggestionBatch, and records exactly what it was
    asked — used to assert the payload never contains row/document data."""

    def __init__(self, response: SuggestionBatch | None = None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.last_system = None
        self.last_payload = None

    def suggest(self, *, system, user_payload, output_schema):
        self.last_system = system
        self.last_payload = user_payload
        if self.error:
            raise self.error
        assert output_schema is SuggestionBatch
        return self.response


def col(name: str, data_type: str, nullable: bool = True) -> ColumnInfo:
    return ColumnInfo(name=name, data_type=data_type, is_nullable=nullable, default=None)


def make_users_table() -> TableSchema:
    return TableSchema(
        name="users",
        columns={
            "id": col("id", "uuid"),
            "email": col("email", "text"),
            "display_name": col("display_name", "text"),
        },
        primary_key=["id"],
    )


def make_users_collection() -> CollectionSchema:
    return CollectionSchema(
        name="users",
        document_count=1,
        sampled_count=1,
        fields={
            "_id": FieldStats(path="_id", bson_types={"objectid"}),
            "email": FieldStats(path="email", bson_types={"string"}),
            "name": FieldStats(path="name", bson_types={"string"}),
        },
        shape_signature_counts={},
        polymorphism_candidate=False,
        discriminator_field=None,
    )


def make_entity() -> EntityMapping:
    return EntityMapping(
        source="users",
        target="users",
        id_strategy=IdStrategy(type=IdStrategyType.OBJECTID_TO_UUID, source_field="_id"),
        fields={"email": FieldSpec(target="email")},
        unmapped=UnmappedPolicy(drop=["name"]),
    )


# --- payload privacy ---------------------------------------------------------------


def test_payload_contains_only_schema_metadata_never_row_data():
    payload = build_llm_payload(
        "users", "users", make_users_collection(), make_users_table(), ["name"], {"email": "email"}, id_column="id"
    )
    serialized = json.dumps(payload)
    # No sample document values anywhere — only names/types/shapes.
    assert "alex@example.com" not in serialized
    assert payload["unresolved_fields"] == [
        {"name": "name", "bson_types": ["string"], "is_array": False, "array_item_kind": None}
    ]
    assert payload["candidate_columns"] == [{"name": "display_name", "data_type": "text", "is_nullable": True}]
    assert payload["already_mapped_fields"] == [{"source_field": "email", "target_column": "email"}]


def test_candidate_columns_excludes_already_used_ones():
    payload = build_llm_payload(
        "users", "users", make_users_collection(), make_users_table(), ["name"], {"email": "email"}, id_column="id"
    )
    names = {c["name"] for c in payload["candidate_columns"]}
    assert "email" not in names  # already claimed
    assert "display_name" in names


def test_candidate_columns_excludes_the_id_column():
    # Regression: id_strategy owns the PK column, not a `fields` entry, so
    # it was never in `already_mapped` — without excluding it explicitly it
    # wrongly looked available for the LLM to suggest another field onto.
    payload = build_llm_payload(
        "users", "users", make_users_collection(), make_users_table(), ["name"], {"email": "email"}, id_column="id"
    )
    names = {c["name"] for c in payload["candidate_columns"]}
    assert "id" not in names


# --- suggest_for_entity --------------------------------------------------------------


def test_suggest_for_entity_returns_none_when_nothing_unresolved():
    entity = EntityMapping(
        source="users",
        target="users",
        id_strategy=IdStrategy(type=IdStrategyType.OBJECTID_TO_UUID, source_field="_id"),
        fields={"email": FieldSpec(target="email")},
    )
    client = _FakeLLMClient()
    result = suggest_for_entity(client, make_users_collection(), make_users_table(), entity)
    assert result is None
    assert client.last_payload is None  # never even called


def test_suggest_for_entity_calls_client_with_schema_only_payload():
    batch = SuggestionBatch(
        suggestions=[FieldSuggestion(source_field="name", action="map", target_column="display_name", reasoning="rename")]
    )
    client = _FakeLLMClient(response=batch)
    result = suggest_for_entity(client, make_users_collection(), make_users_table(), make_entity())
    assert result is batch
    assert client.last_payload["unresolved_fields"][0]["name"] == "name"


# --- apply_suggestions: never trusts the LLM blindly ---------------------------------


def test_map_suggestion_moves_field_from_unmapped_to_fields():
    entity = make_entity()
    table = make_users_table()
    batch = SuggestionBatch(
        suggestions=[FieldSuggestion(source_field="name", action="map", target_column="display_name", reasoning="rename")]
    )
    issues = apply_suggestions("users", entity, table, batch)

    assert entity.fields["name"].target == "display_name"
    assert "name" not in entity.unmapped.drop
    assert any("still requires human confirmation" in i.message for i in issues)


def test_hallucinated_column_is_rejected_not_applied():
    entity = make_entity()
    table = make_users_table()
    batch = SuggestionBatch(
        suggestions=[
            FieldSuggestion(source_field="name", action="map", target_column="nonexistent_column", reasoning="oops")
        ]
    )
    issues = apply_suggestions("users", entity, table, batch)

    assert "name" not in entity.fields
    assert "name" in entity.unmapped.drop  # left exactly as it was
    assert any("not a real column" in i.message for i in issues)


def test_suggestion_targeting_an_already_claimed_column_is_rejected():
    entity = make_entity()
    table = make_users_table()
    batch = SuggestionBatch(
        # "email" is already claimed by the email field — a real, valid
        # column name, but not available for another field to take.
        suggestions=[FieldSuggestion(source_field="name", action="map", target_column="email", reasoning="oops")]
    )
    issues = apply_suggestions("users", entity, table, batch)

    assert "name" not in entity.fields
    assert "name" in entity.unmapped.drop
    assert any("already claimed" in i.message for i in issues)


def test_suggestion_targeting_the_id_column_is_rejected_even_though_id_exists():
    # Regression, mirrors the payload-side one above: id is a real column
    # (would pass the "does this column exist" check) but is exclusively
    # owned by id_strategy, not a valid target for a `fields` entry.
    entity = make_entity()
    table = make_users_table()
    batch = SuggestionBatch(
        suggestions=[FieldSuggestion(source_field="name", action="map", target_column="id", reasoning="non-compliant")]
    )
    issues = apply_suggestions("users", entity, table, batch)

    assert "name" not in entity.fields
    assert "name" in entity.unmapped.drop
    assert any("already claimed" in i.message for i in issues)


def test_split_suggestion_is_surfaced_not_applied():
    entity = make_entity()
    table = make_users_table()
    batch = SuggestionBatch(
        suggestions=[
            FieldSuggestion(
                source_field="name", action="split", split_hint="first_name + last_name", reasoning="looks like a full name"
            )
        ]
    )
    issues = apply_suggestions("users", entity, table, batch)

    assert "name" not in entity.fields  # never applied — DSL can't express it
    assert "name" in entity.unmapped.drop  # left in place for a human to handle
    assert any("split" in i.message.lower() for i in issues)


def test_none_action_leaves_field_unmapped_with_reasoning_surfaced():
    entity = make_entity()
    table = make_users_table()
    batch = SuggestionBatch(
        suggestions=[FieldSuggestion(source_field="name", action="none", reasoning="no good candidate")]
    )
    issues = apply_suggestions("users", entity, table, batch)

    assert "name" not in entity.fields
    assert "name" in entity.unmapped.drop
    assert any("no confident mapping either" in i.message for i in issues)


def test_stale_field_name_in_response_is_ignored():
    entity = make_entity()
    table = make_users_table()
    batch = SuggestionBatch(
        suggestions=[
            FieldSuggestion(source_field="not_a_real_unresolved_field", action="map", target_column="display_name", reasoning="?")
        ]
    )
    issues = apply_suggestions("users", entity, table, batch)
    assert issues == []
    assert "display_name" not in {fs.target for fs in entity.fields.values()}


# --- enrich_mapping_with_llm: failure isolation ---------------------------------------


def test_llm_error_on_one_entity_does_not_abort_the_others():
    good_batch = SuggestionBatch(
        suggestions=[FieldSuggestion(source_field="name", action="map", target_column="display_name", reasoning="rename")]
    )

    class _MixedClient:
        def __init__(self):
            self.calls = 0

        def suggest(self, *, system, user_payload, output_schema):
            self.calls += 1
            if user_payload["mongo_collection"] == "broken":
                raise LLMClientError("simulated failure")
            return good_batch

    mapping = MappingFile(
        entities={
            "users": make_entity(),
            "widgets": EntityMapping(
                source="broken",
                target="widgets",
                id_strategy=IdStrategy(type=IdStrategyType.PASSTHROUGH, source_field="_id"),
                unmapped=UnmappedPolicy(drop=["thing"]),
            ),
        }
    )
    mongo_schemas = {
        "users": make_users_collection(),
        "broken": CollectionSchema(
            name="broken",
            document_count=1,
            sampled_count=1,
            fields={"thing": FieldStats(path="thing", bson_types={"string"})},
            shape_signature_counts={},
            polymorphism_candidate=False,
            discriminator_field=None,
        ),
    }
    from mongopg_migrate.introspect.postgres import PostgresSchema

    pg_schema = PostgresSchema(tables={"users": make_users_table(), "widgets": TableSchema(name="widgets", columns={})})

    client = _MixedClient()
    issues = enrich_mapping_with_llm(client, mapping, mongo_schemas, pg_schema)

    # users succeeded despite widgets failing
    assert mapping.entities["users"].fields["name"].target == "display_name"
    assert any("LLM assist failed" in i.message for i in issues)


def test_llm_client_error_is_a_real_exception_type():
    with pytest.raises(LLMClientError):
        raise LLMClientError("x")
    assert issubclass(LLMClientError, Exception)


def test_field_suggestion_and_batch_are_pydantic_models():
    assert issubclass(FieldSuggestion, BaseModel)
    assert issubclass(SuggestionBatch, BaseModel)
