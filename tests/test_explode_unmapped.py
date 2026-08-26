"""Coverage for the unmapped-field policy's extension one level down, into
`explode` items — closing a gap flagged in the earliest PRD design review,
before any code existed: "nested-path unmapped checks inside exploded
objects... also still unspecified, and acceptable to decide in code."
Confirmed live, re-reading the review verbatim, that this was never
actually decided: introspection has always tracked nested paths like
"items[].discount" (CollectionSchema._walk_document), but nothing ever
checked them against the mapping — an unmapped field inside an exploded
array item was silently dropped, no warning anywhere, the exact bug class
the top-level unmapped-field policy exists to prevent.

`ExplodeSpec.unmapped` mirrors `EntityMapping.unmapped` exactly, including
the same "jsonb is a real landing, not a label" guarantee — adding the
disposition without wiring load.py to actually write it would just
reproduce the original top-level jsonb-no-op bug one level down, so both
are covered here.
"""

import mongomock
import pytest

from mongopg_migrate.introspect.postgres import ColumnInfo, PostgresSchema, TableSchema
from mongopg_migrate.mapping.schema import (
    EntityMapping,
    ExplodeSpec,
    FieldSpec,
    ForeignKeyRef,
    IdStrategy,
    IdStrategyType,
    MappingFile,
    UnmappedPolicy,
    _field_names_at_path,
    validate_explode_field_coverage,
)
from mongopg_migrate.migrate.load import _collect_explode_rows, _load_entity_batches


def col(name: str, data_type: str, nullable: bool = True) -> ColumnInfo:
    return ColumnInfo(name=name, data_type=data_type, is_nullable=nullable, default=None)


# --- ExplodeSpec.mapped_item_fields / UnmappedPolicy reuse -----------------------------------


def test_explode_spec_accepts_unmapped_policy():
    exp = ExplodeSpec(
        target="order_items",
        id_strategy=IdStrategy(type=IdStrategyType.SERIAL),
        parent_fk=ForeignKeyRef(target_field="order_id", references="orders.id"),
        fields={"qty": FieldSpec(target="qty")},
        unmapped=UnmappedPolicy(drop=["note"]),
    )
    assert exp.mapped_item_fields() == {"qty", "note"}


def test_explode_spec_jsonb_without_jsonb_column_rejected():
    with pytest.raises(ValueError, match="jsonb_column"):
        ExplodeSpec(
            target="order_items",
            id_strategy=IdStrategy(type=IdStrategyType.SERIAL),
            parent_fk=ForeignKeyRef(target_field="order_id", references="orders.id"),
            unmapped=UnmappedPolicy(jsonb=["note"]),  # no jsonb_column
        )


def test_explode_spec_mapped_item_fields_auto_accounts_id_strategy_source_field_when_nested():
    exp = ExplodeSpec(
        target="hospital_facilities",
        id_strategy=IdStrategy(type=IdStrategyType.OBJECTID_TO_UUID, source_field="facilityId"),
        parent_fk=ForeignKeyRef(target_field="hospital_id", references="hospitals.id"),
        explode={
            "categoryParts": ExplodeSpec(
                target="facility_category_parts",
                id_strategy=IdStrategy(type=IdStrategyType.SERIAL),
                parent_fk=ForeignKeyRef(target_field="facility_id", references="hospital_facilities.id"),
            )
        },
    )
    # facilityId is the id_strategy source_field, not a `fields:` entry —
    # must still count as accounted for, same as `_id` at the top level.
    assert "facilityId" in exp.mapped_item_fields()
    assert "categoryParts" in exp.mapped_item_fields()


# --- _field_names_at_path ---------------------------------------------------------------------


def test_field_names_at_path_extracts_immediate_names_only():
    paths = {"items[].productId", "items[].qty", "items[].meta.color", "otherField"}
    names = _field_names_at_path(paths, "items[].")
    assert names == {"productId", "qty", "meta"}  # "meta.color" collapses to just "meta"


def test_field_names_at_path_handles_nested_array_prefix():
    paths = {"facilities[].categoryParts[].code", "facilities[].name"}
    names = _field_names_at_path(paths, "facilities[].categoryParts[].")
    assert names == {"code"}


# --- validate_explode_field_coverage --------------------------------------------------------


def _orders_mapping(explode_fields=None, explode_unmapped=None) -> MappingFile:
    return MappingFile(
        entities={
            "orders": EntityMapping(
                source="orders",
                target="orders",
                id_strategy=IdStrategy(type=IdStrategyType.OBJECTID_TO_UUID, source_field="_id"),
                explode={
                    "items": ExplodeSpec(
                        target="order_items",
                        id_strategy=IdStrategy(type=IdStrategyType.SERIAL),
                        parent_fk=ForeignKeyRef(target_field="order_id", references="orders.id"),
                        fields=explode_fields or {"qty": FieldSpec(target="qty")},
                        unmapped=explode_unmapped or UnmappedPolicy(),
                    )
                },
            )
        }
    )


def test_explode_field_coverage_flags_unmapped_field_inside_array_item():
    mapping = _orders_mapping()
    issues = validate_explode_field_coverage(mapping, {"orders": {"items[].qty", "items[].discount"}})
    assert len(issues) == 1
    assert issues[0].severity == "error"
    assert issues[0].field == "discount"
    assert issues[0].entity == "orders.items"


def test_explode_field_coverage_clean_when_everything_dispositioned():
    mapping = _orders_mapping(
        explode_fields={"qty": FieldSpec(target="qty")},
        explode_unmapped=UnmappedPolicy(drop=["discount"]),
    )
    issues = validate_explode_field_coverage(mapping, {"orders": {"items[].qty", "items[].discount"}})
    assert issues == []


def test_explode_field_coverage_skips_entities_with_no_explode():
    mapping = MappingFile(
        entities={
            "users": EntityMapping(
                source="users", target="users",
                id_strategy=IdStrategy(type=IdStrategyType.OBJECTID_TO_UUID, source_field="_id"),
            )
        }
    )
    assert validate_explode_field_coverage(mapping, {"users": {"name", "email"}}) == []


def test_explode_field_coverage_recurses_into_nested_explode():
    mapping = MappingFile(
        entities={
            "hospitals": EntityMapping(
                source="hospitals", target="hospitals",
                id_strategy=IdStrategy(type=IdStrategyType.OBJECTID_TO_UUID, source_field="_id"),
                explode={
                    "facilities": ExplodeSpec(
                        target="hospital_facilities",
                        id_strategy=IdStrategy(type=IdStrategyType.OBJECTID_TO_UUID, source_field="facilityId"),
                        parent_fk=ForeignKeyRef(target_field="hospital_id", references="hospitals.id"),
                        fields={"name": FieldSpec(target="name")},
                        explode={
                            "categoryParts": ExplodeSpec(
                                target="facility_category_parts",
                                id_strategy=IdStrategy(type=IdStrategyType.SERIAL),
                                parent_fk=ForeignKeyRef(target_field="facility_id", references="hospital_facilities.id"),
                                fields={"partName": FieldSpec(target="part_name")},
                            )
                        },
                    )
                },
            )
        }
    )
    paths = {
        "facilities[].facilityId", "facilities[].name",
        "facilities[].categoryParts[].partName", "facilities[].categoryParts[].code",  # unmapped
    }
    issues = validate_explode_field_coverage(mapping, {"hospitals": paths})
    assert len(issues) == 1
    assert issues[0].field == "code"
    assert issues[0].entity == "hospitals.facilities.categoryParts"


def test_explode_field_coverage_missing_schema_entry_is_skipped_not_errored():
    # validate_against_mongo_schema already warns "no introspected schema
    # found" for this case — this function shouldn't double up on it.
    mapping = _orders_mapping()
    assert validate_explode_field_coverage(mapping, {}) == []


# --- load.py: explode-level unmapped.jsonb lands for real, not just a label ------------------


def _pg_schema_with_jsonb() -> PostgresSchema:
    return PostgresSchema(
        tables={
            "orders": TableSchema(name="orders", columns={"id": col("id", "uuid")}, primary_key=["id"]),
            "order_items": TableSchema(
                name="order_items",
                columns={
                    "order_id": col("order_id", "uuid"),
                    "qty": col("qty", "integer"),
                    "extra": col("extra", "jsonb"),
                },
                primary_key=["id"],
            ),
        }
    )


def test_collect_explode_rows_lands_jsonb_payload():
    exp = ExplodeSpec(
        target="order_items",
        id_strategy=IdStrategy(type=IdStrategyType.SERIAL),
        parent_fk=ForeignKeyRef(target_field="order_id", references="orders.id"),
        fields={"qty": FieldSpec(target="qty")},
        unmapped=UnmappedPolicy(jsonb=["discount", "note"], jsonb_column="extra"),
    )
    item = {"qty": 2, "discount": 0.1, "note": "gift wrap"}
    explode_rows = {"items": []}
    _collect_explode_rows(
        "order-uuid", [item], exp, path="items", context="orders", conn=None,
        pg_schema=_pg_schema_with_jsonb(), id_buffers={}, explode_rows=explode_rows,
        internal_schema="_mongopg", external_conns=None,
    )
    assert len(explode_rows["items"]) == 1
    row = explode_rows["items"][0]
    # row = [parent_fk, qty, jsonb_payload] — no own id (leaf, SERIAL)
    assert row[0] == "order-uuid"
    assert row[1] == 2
    jsonb_payload = row[2]
    assert jsonb_payload.obj == {"discount": 0.1, "note": "gift wrap"}


def test_load_entity_batches_raises_when_explode_jsonb_column_missing_from_schema():
    from mongopg_migrate.migrate.load import LoadError

    exp = ExplodeSpec(
        target="order_items",
        id_strategy=IdStrategy(type=IdStrategyType.SERIAL),
        parent_fk=ForeignKeyRef(target_field="order_id", references="orders.id"),
        unmapped=UnmappedPolicy(jsonb=["note"], jsonb_column="not_a_real_column"),
    )
    entity = EntityMapping(
        source="orders", target="orders",
        id_strategy=IdStrategy(type=IdStrategyType.OBJECTID_TO_UUID, source_field="_id"),
        explode={"items": exp},
    )
    client = mongomock.MongoClient()
    db = client["app"]
    db.orders.insert_one({"_id": mongomock.ObjectId(), "items": [{"note": "x"}]})

    with pytest.raises(LoadError, match="not_a_real_column"):
        _load_entity_batches(
            None, db.orders, "orders", entity, _pg_schema_with_jsonb(), batch_size=10,
        )
    client.close()
