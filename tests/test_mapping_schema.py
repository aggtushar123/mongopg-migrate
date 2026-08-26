from pathlib import Path

import pytest
from pydantic import ValidationError

from mongopg_migrate.mapping.schema import (
    EntityMapping,
    ExplodeSpec,
    FieldSpec,
    FilterSpec,
    ForeignKeyRef,
    IdStrategy,
    IdStrategyType,
    MappingFile,
    UnmappedPolicy,
    load_mapping_file,
    validate_against_mongo_schema,
    validate_collection_coverage,
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


def test_validate_structure_flags_unknown_lookup_entity_nested_two_levels_deep():
    # Regression: a typo'd lookup one level deeper than the top explode
    # level (facilities[].categoryParts[].lookup:) passed validate_structure
    # with zero issues before this fix — found live, testing directly.
    mapping = MappingFile(
        entities={
            "hospitals": EntityMapping(
                source="hospitals",
                target="hospitals",
                id_strategy=IdStrategy(type=IdStrategyType.OBJECTID_TO_UUID, source_field="_id"),
                explode={
                    "facilities": ExplodeSpec(
                        target="hospital_facilities",
                        id_strategy=IdStrategy(type=IdStrategyType.OBJECTID_TO_UUID, source_field="facilityId"),
                        parent_fk=ForeignKeyRef(target_field="hospital_id", references="hospitals.id"),
                        explode={
                            "categoryParts": ExplodeSpec(
                                target="facility_category_parts",
                                id_strategy=IdStrategy(type=IdStrategyType.SERIAL),
                                parent_fk=ForeignKeyRef(
                                    target_field="facility_id", references="hospital_facilities.id"
                                ),
                                fields={
                                    "categoryId": FieldSpec(target="category_id", lookup="zcategoriezz_TYPO")
                                },
                            )
                        },
                    )
                },
            )
        }
    )
    issues = validate_structure(mapping)
    assert len(issues) == 1
    assert issues[0].field == "facilities.categoryParts.categoryId"
    assert "zcategoriezz_TYPO" in issues[0].message


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


def _widgets_mapping() -> MappingFile:
    return MappingFile(
        entities={
            "widgets": EntityMapping(
                source="widgets",
                target="widgets",
                id_strategy=IdStrategy(type=IdStrategyType.PASSTHROUGH, source_field="_id"),
                fields={"name": "name"},
            )
        }
    )


# --- validate_collection_coverage -------------------------------------------------------------
#
# "A collection simply absent from the mapping file is never mentioned by
# any command. 'Deliberately not migrating this' and 'forgot this existed'
# are indistinguishable." — found by testing directly against the tool's
# actual validation surface: no existing check ever compares the mapping
# file's entities against the full list of collections in the source
# database, only fields *within* an already-mapped entity.


def test_collection_coverage_flags_an_unmapped_collection():
    mapping = _widgets_mapping()
    issues = validate_collection_coverage(mapping, {"widgets", "auditLogs"})
    assert len(issues) == 1
    assert issues[0].severity == "warning"
    assert issues[0].entity is None
    assert "auditLogs" in issues[0].message


def test_collection_coverage_clean_when_every_collection_is_mapped():
    mapping = _widgets_mapping()
    issues = validate_collection_coverage(mapping, {"widgets"})
    assert issues == []


def test_collection_coverage_respects_excluded_collections():
    mapping = MappingFile(
        entities=_widgets_mapping().entities,
        excluded_collections=["auditLogs"],
    )
    issues = validate_collection_coverage(mapping, {"widgets", "auditLogs"})
    assert issues == []


def test_collection_coverage_multiple_entities_sharing_one_source_are_both_accounted():
    # A discriminator-filtered pair sharing one source collection must not
    # both count as "the same collection, still uncovered" — one shared
    # `source:` value covers the collection regardless of how many entities
    # split it.
    mapping = MappingFile(
        entities={
            "payments_card": EntityMapping(
                source="payments",
                target="payments_card",
                id_strategy=IdStrategy(type=IdStrategyType.PASSTHROUGH, source_field="_id"),
                filter=FilterSpec(field="type", equals="card"),
            ),
            "payments_cash": EntityMapping(
                source="payments",
                target="payments_cash",
                id_strategy=IdStrategy(type=IdStrategyType.PASSTHROUGH, source_field="_id"),
                filter=FilterSpec(field="type", equals="cash"),
            ),
        }
    )
    issues = validate_collection_coverage(mapping, {"payments"})
    assert issues == []


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
