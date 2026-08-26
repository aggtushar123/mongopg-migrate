import pytest

from mongopg_migrate.introspect.postgres import (
    CircularDependencyError,
    ForeignKey,
    PostgresSchema,
    TableSchema,
)


def _fk(column: str, references_table: str, references_column: str = "id", deferrable: bool = False) -> ForeignKey:
    return ForeignKey(
        constraint_name=f"fk_{column}",
        column=column,
        references_table=references_table,
        references_column=references_column,
        is_deferrable=deferrable,
    )


def test_linear_chain_orders_parents_before_children():
    schema = PostgresSchema(
        tables={
            "users": TableSchema(name="users"),
            "orders": TableSchema(name="orders", foreign_keys=[_fk("user_id", "users")]),
            "order_items": TableSchema(name="order_items", foreign_keys=[_fk("order_id", "orders")]),
        }
    )
    batches = schema.load_order()
    order = [t for batch in batches for t in batch]
    assert order.index("users") < order.index("orders") < order.index("order_items")
    assert all(len(b) == 1 for b in batches)


def test_self_reference_is_not_a_cycle():
    schema = PostgresSchema(
        tables={
            "categories": TableSchema(
                name="categories", foreign_keys=[_fk("parent_id", "categories")]
            ),
        }
    )
    assert schema.load_order() == [["categories"]]


def test_deferrable_cycle_loads_as_one_batch():
    schema = PostgresSchema(
        tables={
            "a": TableSchema(name="a", foreign_keys=[_fk("b_id", "b", deferrable=True)]),
            "b": TableSchema(name="b", foreign_keys=[_fk("a_id", "a", deferrable=True)]),
        }
    )
    batches = schema.load_order()
    assert batches == [["a", "b"]]


def test_non_deferrable_cycle_raises():
    schema = PostgresSchema(
        tables={
            "a": TableSchema(name="a", foreign_keys=[_fk("b_id", "b", deferrable=False)]),
            "b": TableSchema(name="b", foreign_keys=[_fk("a_id", "a", deferrable=True)]),
        }
    )
    with pytest.raises(CircularDependencyError):
        schema.load_order()
