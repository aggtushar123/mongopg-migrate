from pathlib import Path

import pytest
from pydantic import ValidationError

from mongopg_migrate.mapping.schema import (
    EntityMapping,
    FieldSpec,
    IdStrategy,
    IdStrategyType,
    MappingFile,
    UnmappedPolicy,
    load_mapping_file,
    validate_against_mongo_schema,
    validate_structure,
)

FIXTURE_MAPPING = Path(__file__).parent.parent / "fixtures" / "mapping.example.yaml"


def test_loads_the_prd_worked_example():
    mapping = load_mapping_file(FIXTURE_MAPPING)
    assert set(mapping.entities) == {"users", "products", "tags", "orders"}
    orders = mapping.entities["orders"]
    assert orders.id_strategy.type == IdStrategyType.OBJECTID_TO_UUID
    assert orders.fields["userId"].lookup == "users"
    assert "items" in orders.explode
    assert orders.explode["items"].target == "order_items"
    assert orders.explode["items"].parent_fk.references == "orders.id"
    assert "tagIds" in orders.junction
    assert orders.junction["tagIds"].child_fk.lookup == "tags"


def test_field_shorthand_string_is_normalized():
    entity = EntityMapping(
        source="widgets",
        target="widgets",
        id_strategy=IdStrategy(type=IdStrategyType.PASSTHROUGH, source_field="_id"),
        fields={"name": "name"},
    )
    assert entity.fields["name"] == FieldSpec(target="name")


def test_serial_id_strategy_does_not_require_source_field():
    IdStrategy(type=IdStrategyType.SERIAL)  # should not raise


def test_non_serial_id_strategy_requires_source_field():
    with pytest.raises(ValidationError):
        IdStrategy(type=IdStrategyType.OBJECTID_TO_UUID)


def test_unmapped_fields_shorthand_accepts_only_empty_list():
    EntityMapping(
        source="widgets",
        target="widgets",
        id_strategy=IdStrategy(type=IdStrategyType.SERIAL),
        unmapped=[],  # type: ignore[arg-type]
    )
    with pytest.raises(ValidationError):
        EntityMapping(
            source="widgets",
            target="widgets",
            id_strategy=IdStrategy(type=IdStrategyType.SERIAL),
            unmapped=["leftover_field"],  # type: ignore[arg-type]
        )


def test_unmapped_policy_rejects_overlap():
    with pytest.raises(ValidationError):
        UnmappedPolicy(drop=["a"], jsonb=["a"])


def test_validate_structure_flags_unknown_lookup_entity():
    mapping = MappingFile(
        entities={
            "widgets": EntityMapping(
                source="widgets",
                target="widgets",
                id_strategy=IdStrategy(type=IdStrategyType.PASSTHROUGH, source_field="_id"),
                fields={"ownerId": FieldSpec(target="owner_id", lookup="nonexistent_entity")},
            )
        }
    )
    issues = validate_structure(mapping)
    assert any("nonexistent_entity" in i.message for i in issues)


def test_unmapped_field_policy_catches_a_field_with_no_disposition():
    mapping = MappingFile(
        entities={
            "widgets": EntityMapping(
                source="widgets",
                target="widgets",
                id_strategy=IdStrategy(type=IdStrategyType.PASSTHROUGH, source_field="_id"),
                fields={"name": "name"},
                # `description` is present on the source but never given a disposition
            )
        }
    )
    issues = validate_against_mongo_schema(mapping, {"widgets": {"name", "description"}})
    errors = [i for i in issues if i.severity == "error"]
    assert len(errors) == 1
    assert errors[0].field == "description"


def test_unmapped_field_policy_passes_when_everything_has_a_disposition():
    mapping = MappingFile(
        entities={
            "widgets": EntityMapping(
                source="widgets",
                target="widgets",
                id_strategy=IdStrategy(type=IdStrategyType.PASSTHROUGH, source_field="_id"),
                fields={"name": "name"},
                unmapped=UnmappedPolicy(drop=["internal_notes"]),
            )
        }
    )
    issues = validate_against_mongo_schema(mapping, {"widgets": {"name", "internal_notes"}})
    assert not [i for i in issues if i.severity == "error"]
