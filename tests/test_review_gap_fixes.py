"""Regression tests for the five ranked gaps from the code review:

1. unmapped.jsonb silent no-op -> jsonb_column required, actually landed.
2. validate_against_mongo_schema key mismatch (entity.source vs entity name).
3. Discriminator-filtered mappings (FilterSpec / mongo_filter()).
4. Cross-run lookups (external_entities).
5. append/upsert skipping already-'done' entities (covered live in Docker,
   not here — needs a real checkpoint + Mongo cursor).
"""

import pytest
from pydantic import ValidationError

from mongopg_migrate.mapping.schema import (
    EntityMapping,
    FieldSpec,
    FilterSpec,
    IdStrategy,
    IdStrategyType,
    MappingFile,
    UnmappedPolicy,
    validate_against_mongo_schema,
    validate_structure,
)


def _entity(source="widgets", **kwargs) -> EntityMapping:
    return EntityMapping(
        source=source,
        target=kwargs.pop("target", source),
        id_strategy=kwargs.pop("id_strategy", IdStrategy(type=IdStrategyType.OBJECTID_TO_UUID, source_field="_id")),
        **kwargs,
    )


# --- Gap 1: unmapped.jsonb no-op ------------------------------------------------------


def test_jsonb_fields_without_jsonb_column_is_rejected():
    with pytest.raises(ValidationError, match="jsonb_column is not set"):
        UnmappedPolicy(jsonb=["extra_field"])


def test_jsonb_column_without_jsonb_fields_is_rejected():
    with pytest.raises(ValidationError, match="nothing to land there"):
        UnmappedPolicy(jsonb_column="extra")


def test_jsonb_with_matching_column_is_valid():
    policy = UnmappedPolicy(jsonb=["a", "b"], jsonb_column="extra")
    assert policy.jsonb_column == "extra"


def test_empty_unmapped_policy_is_still_valid():
    UnmappedPolicy()  # no jsonb, no jsonb_column — fine


# --- Gap 2: validate_against_mongo_schema key mismatch ------------------------------


def test_unmapped_field_policy_uses_entity_name_when_source_differs():
    # Regression: this used to look up mongo_fields_by_entity by
    # entity.source, but the dict here (as introspect_database + cli.py
    # actually produce it) is keyed by entity NAME. name != source is
    # exactly the discriminator-filtered case (gap 3) — two entities
    # sharing one source collection can't both be keyed by source anyway.
    mapping = MappingFile(
        entities={
            "widgets_v2": EntityMapping(
                source="widgets",  # name != source
                target="widgets",
                id_strategy=IdStrategy(type=IdStrategyType.OBJECTID_TO_UUID, source_field="_id"),
                fields={"name": FieldSpec(target="name")},
            )
        }
    )
    # Keyed by entity NAME ("widgets_v2"), not source ("widgets") — matches
    # how cli.py actually builds this dict.
    fields_by_entity = {"widgets_v2": {"name", "description"}}

    issues = validate_against_mongo_schema(mapping, fields_by_entity)
    errors = [i for i in issues if i.severity == "error"]
    warnings = [i for i in issues if i.severity == "warning"]

    # The real check must have run (found the unmapped "description" field),
    # not silently degraded to "no introspected schema found".
    assert not warnings
    assert len(errors) == 1
    assert errors[0].field == "description"


# --- Gap 3: discriminator-filtered mappings -----------------------------------------


def test_entity_with_no_filter_has_empty_mongo_filter():
    assert _entity().mongo_filter() == {}


def test_entity_with_filter_produces_matching_mongo_query():
    entity = _entity(filter=FilterSpec(field="type", equals="card"))
    assert entity.mongo_filter() == {"type": "card"}


def test_filter_accepts_string_int_and_bool_equals():
    assert FilterSpec(field="type", equals="card").equals == "card"
    assert FilterSpec(field="version", equals=2).equals == 2
    assert FilterSpec(field="active", equals=True).equals is True


# --- Gap 4: cross-run / external lookups --------------------------------------------


def test_lookup_to_undeclared_entity_is_an_error_by_default():
    mapping = MappingFile(
        entities={
            "orders": _entity(
                source="orders", fields={"userId": FieldSpec(target="user_id", lookup="users")}
            )
        }
    )
    issues = validate_structure(mapping)
    assert any("not a declared entity" in i.message for i in issues)


def test_lookup_to_declared_external_entity_is_not_an_error():
    mapping = MappingFile(
        entities={
            "orders": _entity(
                source="orders", fields={"userId": FieldSpec(target="user_id", lookup="users")}
            )
        },
        external_entities=["users"],
    )
    issues = validate_structure(mapping)
    assert issues == []


def test_external_entities_does_not_mask_a_genuine_typo():
    # Declaring `users` external doesn't make an unrelated typo'd lookup
    # ("usres") pass too — external_entities is an allowlist, not a blanket
    # "trust every unknown lookup" switch.
    mapping = MappingFile(
        entities={
            "orders": _entity(
                source="orders", fields={"userId": FieldSpec(target="user_id", lookup="usres")}
            )
        },
        external_entities=["users"],
    )
    issues = validate_structure(mapping)
    assert any("usres" in i.message for i in issues)
