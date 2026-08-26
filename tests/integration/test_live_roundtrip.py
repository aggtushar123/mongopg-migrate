"""The core loop, proven against real Mongo + real Postgres, through the
actual CLI surface (click's CliRunner — in-process, but the exact same
`main` group a user invokes) rather than calling internal functions
directly the way the unit tests do. Re-creates the PRD §12 worked example
fixture from scratch each run (own tables/collections, `_roundtrip_test`
suffixed, cleaned up after) so this doesn't collide with anything a
developer might have loaded into the same compose stack by hand.
"""

from __future__ import annotations

import datetime

import pytest
from bson import ObjectId
from click.testing import CliRunner

from mongopg_migrate.cli import main

USER_ID = ObjectId("64f0b1000000000000000001")
PRODUCT_ID = ObjectId("64e9c3000000000000000001")
TAG_A = ObjectId("64e0aa000000000000000001")
TAG_B = ObjectId("64e0ab000000000000000002")
ORDER_1 = ObjectId("64f1a2000000000000000001")
ORDER_2 = ObjectId("64f1a2000000000000000002")

MAPPING_YAML = """
entities:
  users:
    source: users_roundtrip_test
    target: users_roundtrip_test
    id_strategy: {type: objectid_to_uuid, source_field: _id}
    fields:
      email: email
      name: name
  products:
    source: products_roundtrip_test
    target: products_roundtrip_test
    id_strategy: {type: objectid_to_uuid, source_field: _id}
    fields:
      sku: sku
      name: name
  tags:
    source: tags_roundtrip_test
    target: tags_roundtrip_test
    id_strategy: {type: objectid_to_uuid, source_field: _id}
    fields:
      label: label
  orders:
    source: orders_roundtrip_test
    target: orders_roundtrip_test
    id_strategy: {type: objectid_to_uuid, source_field: _id}
    fields:
      status: status
      createdAt: {target: created_at, transform: cast_timestamptz}
      userId: {target: user_id, lookup: users}
    explode:
      items:
        target: order_items_roundtrip_test
        id_strategy: {type: serial}
        parent_fk: {target_field: order_id, references: orders_roundtrip_test.id}
        fields:
          productId: {target: product_id, lookup: products}
          qty: qty
          price: price
    junction:
      tagIds:
        target: order_tags_roundtrip_test
        parent_fk: {target_field: order_id, references: orders_roundtrip_test.id}
        child_fk: {target_field: tag_id, references: tags_roundtrip_test.id, lookup: tags}
"""


@pytest.fixture
def seeded(mongo_db, pg_conn):
    mongo_db.users_roundtrip_test.insert_one({"_id": USER_ID, "email": "alex@example.com", "name": "Alex Rivera"})
    mongo_db.products_roundtrip_test.insert_one({"_id": PRODUCT_ID, "sku": "WIDGET-1", "name": "Widget"})
    mongo_db.tags_roundtrip_test.insert_many([{"_id": TAG_A, "label": "priority"}, {"_id": TAG_B, "label": "gift"}])
    mongo_db.orders_roundtrip_test.insert_many(
        [
            {
                "_id": ORDER_1,
                "userId": USER_ID,
                "status": "shipped",
                "createdAt": datetime.datetime(2026, 1, 4, 10, 0, tzinfo=datetime.UTC),
                "items": [{"productId": PRODUCT_ID, "qty": 2, "price": 19.99}],
                "tagIds": [TAG_A, TAG_B],
            },
            {
                "_id": ORDER_2,
                "userId": USER_ID,
                "status": "pending",
                "createdAt": datetime.datetime(2026, 1, 5, 9, 30, tzinfo=datetime.UTC),
                "items": [{"productId": PRODUCT_ID, "qty": 1, "price": 19.99}],
                "tagIds": [TAG_A],
            },
        ]
    )

    with pg_conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE users_roundtrip_test (id UUID PRIMARY KEY, email TEXT NOT NULL, name TEXT NOT NULL);
            CREATE TABLE products_roundtrip_test (id UUID PRIMARY KEY, sku TEXT NOT NULL, name TEXT NOT NULL);
            CREATE TABLE tags_roundtrip_test (id UUID PRIMARY KEY, label TEXT NOT NULL);
            CREATE TABLE orders_roundtrip_test (
                id UUID PRIMARY KEY,
                user_id UUID NOT NULL REFERENCES users_roundtrip_test(id),
                status TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL
            );
            CREATE TABLE order_items_roundtrip_test (
                id SERIAL PRIMARY KEY,
                order_id UUID NOT NULL REFERENCES orders_roundtrip_test(id),
                product_id UUID NOT NULL REFERENCES products_roundtrip_test(id),
                qty INT NOT NULL,
                price NUMERIC NOT NULL
            );
            CREATE TABLE order_tags_roundtrip_test (
                order_id UUID NOT NULL REFERENCES orders_roundtrip_test(id),
                tag_id UUID NOT NULL REFERENCES tags_roundtrip_test(id),
                PRIMARY KEY (order_id, tag_id)
            );
        """)

    yield

    with pg_conn.cursor() as cur:
        cur.execute("""
            DROP TABLE IF EXISTS order_tags_roundtrip_test, order_items_roundtrip_test,
                orders_roundtrip_test, tags_roundtrip_test, products_roundtrip_test, users_roundtrip_test;
        """)
        cur.execute(
            "DELETE FROM _mongopg.load_checkpoint WHERE entity IN "
            "('users','products','tags','orders')"
        )
        cur.execute(
            "DELETE FROM _mongopg.id_map WHERE entity IN "
            "('users','products','tags','orders')"
        )
    for coll in ["users_roundtrip_test", "products_roundtrip_test", "tags_roundtrip_test", "orders_roundtrip_test"]:
        mongo_db[coll].drop()


def _run(args: list[str], mongo_uri: str, postgres_uri: str):
    runner = CliRunner()
    return runner.invoke(
        main,
        args,
        env={"MONGO_URI": mongo_uri, "POSTGRES_URI": postgres_uri},
        catch_exceptions=False,
    )


def test_full_pipeline_against_real_mongo_and_postgres(seeded, mongo_uri, postgres_uri, tmp_path):
    mapping_path = tmp_path / "mapping.yaml"
    mapping_path.write_text(MAPPING_YAML)

    result = _run(["validate-mapping", str(mapping_path)], mongo_uri, postgres_uri)
    assert result.exit_code == 0, result.output

    result = _run(["dry-run", str(mapping_path)], mongo_uri, postgres_uri)
    assert result.exit_code == 0, result.output
    assert "no violations" in result.output

    result = _run(["migrate", str(mapping_path), "--mode", "truncate"], mongo_uri, postgres_uri)
    assert result.exit_code == 0, result.output

    result = _run(["validate", str(mapping_path)], mongo_uri, postgres_uri)
    assert result.exit_code == 0, result.output
    assert "OK — counts match" in result.output
    assert "MISMATCH" not in result.output
