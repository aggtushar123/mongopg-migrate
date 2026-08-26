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


# --- load_mapping_file: duplicate-key safety --------------------------------------------------
#
# yaml.safe_load's default behavior for a duplicate mapping key is silent
# last-one-wins — found live, writing a mapping with the same source field
# key twice under `fields:` (once resolved via lookup, once as a raw
# passthrough copy): the first entry vanished with zero warning. Same
# footgun class as the scalar-iteration and bare-`null` bugs already fixed
# elsewhere in this tool.


def test_load_mapping_file_rejects_duplicate_field_key(tmp_path):
    p = tmp_path / "mapping.yaml"
    p.write_text(
        """
        entities:
          orders:
            source: orders
            target: orders
            id_strategy: {type: objectid_to_uuid, source_field: _id}
            fields:
              mcmUserId: {target: user_id, lookup: mcm_user}
              mcmUserId: {target: legacy_mcm_user_id}
        """
    )
    with pytest.raises(ValueError, match="duplicate key 'mcmUserId'"):
        load_mapping_file(p)


def test_load_mapping_file_rejects_duplicate_entity_key(tmp_path):
    p = tmp_path / "mapping.yaml"
    p.write_text(
        """
        entities:
          orders:
            source: orders_v1
            target: orders
            id_strategy: {type: objectid_to_uuid, source_field: _id}
          orders:
            source: orders_v2
            target: orders
            id_strategy: {type: objectid_to_uuid, source_field: _id}
        """
    )
    with pytest.raises(ValueError, match="duplicate key 'orders'"):
        load_mapping_file(p)


def test_load_mapping_file_accepts_a_clean_file_with_no_duplicates(tmp_path):
    p = tmp_path / "mapping.yaml"
    p.write_text(
        """
        entities:
          orders:
            source: orders
            target: orders
            id_strategy: {type: objectid_to_uuid, source_field: _id}
            fields:
              status: status
              userId: {target: user_id, lookup: users}
        """
    )
    mapping = load_mapping_file(p)
    assert set(mapping.entities) == {"orders"}
    assert set(mapping.entities["orders"].fields) == {"status", "userId"}
