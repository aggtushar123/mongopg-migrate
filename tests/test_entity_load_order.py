import pytest

from mongopg_migrate.mapping.schema import (
    CircularEntityDependencyError,
    EntityMapping,
    ExplodeSpec,
    FieldSpec,
    ForeignKeyRef,
    IdStrategy,
    IdStrategyType,
    JunctionSpec,
    MappingFile,
)


def _entity(source: str, fields=None, explode=None, junction=None) -> EntityMapping:
    return EntityMapping(
        source=source,
        target=source,
        id_strategy=IdStrategy(type=IdStrategyType.OBJECTID_TO_UUID, source_field="_id"),
        fields=fields or {},
        explode=explode or {},
        junction=junction or {},
    )


def test_order_respects_direct_lookup_dependency():
    mapping = MappingFile(
        entities={
            "orders": _entity("orders", fields={"userId": FieldSpec(target="user_id", lookup="users")}),
            "users": _entity("users"),
        }
    )
    order = mapping.entity_load_order()
    assert order.index("users") < order.index("orders")


def test_order_respects_explode_child_field_lookup():
    mapping = MappingFile(
        entities={
            "orders": _entity(
                "orders",
                explode={
                    "items": ExplodeSpec(
                        target="order_items",
                        id_strategy=IdStrategy(type=IdStrategyType.SERIAL),
                        parent_fk=ForeignKeyRef(target_field="order_id", references="orders.id"),
                        fields={"productId": FieldSpec(target="product_id", lookup="products")},
                    )
                },
            ),
            "products": _entity("products"),
        }
    )
    order = mapping.entity_load_order()
    assert order.index("products") < order.index("orders")


def test_order_respects_junction_child_fk_lookup():
    mapping = MappingFile(
        entities={
            "orders": _entity(
                "orders",
                junction={
                    "tagIds": JunctionSpec(
                        target="order_tags",
                        parent_fk=ForeignKeyRef(target_field="order_id", references="orders.id"),
                        child_fk=ForeignKeyRef(target_field="tag_id", references="tags.id", lookup="tags"),
                    )
                },
            ),
            "tags": _entity("tags"),
        }
    )
    order = mapping.entity_load_order()
    assert order.index("tags") < order.index("orders")


def test_circular_lookup_dependency_raises():
    mapping = MappingFile(
        entities={
            "a": _entity("a", fields={"bId": FieldSpec(target="b_id", lookup="b")}),
            "b": _entity("b", fields={"aId": FieldSpec(target="a_id", lookup="a")}),
        }
    )
    with pytest.raises(CircularEntityDependencyError):
        mapping.entity_load_order()
