"""Regression test for a real bug caught in live testing: Postgres refuses
TRUNCATE on a table with incoming FKs unless every referencing table is
truncated in the *same* statement — truncating child-then-parent as
separate statements (even in the right order) still fails. See
migrate/load.py:_truncate_mapped_tables.
"""

from mongopg_migrate.mapping.schema import (
    EntityMapping,
    ExplodeSpec,
    ForeignKeyRef,
    IdStrategy,
    IdStrategyType,
    MappingFile,
)
from mongopg_migrate.migrate.load import _truncate_mapped_tables


class _FakeCursor:
    def __init__(self, log: list[str]):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql: str, params=None):
        self.log.append(sql)


class _FakeConn:
    def __init__(self):
        self.executed: list[str] = []

    def cursor(self):
        return _FakeCursor(self.executed)


def test_truncate_issues_a_single_statement_for_all_mapped_tables():
    mapping = MappingFile(
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
                    )
                },
            )
        }
    )
    conn = _FakeConn()
    _truncate_mapped_tables(conn, mapping)

    assert len(conn.executed) == 1, "must be one TRUNCATE statement, not one per table"
    sql = conn.executed[0]
    assert sql.startswith("TRUNCATE TABLE ")
    assert '"order_items"' in sql
    assert '"orders"' in sql
    assert "CASCADE" not in sql


def test_truncate_is_a_noop_for_an_empty_mapping():
    conn = _FakeConn()
    _truncate_mapped_tables(conn, MappingFile(entities={}))
    assert conn.executed == []
