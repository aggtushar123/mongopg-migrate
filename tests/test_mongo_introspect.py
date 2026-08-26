import mongomock

from mongopg_migrate.introspect.mongo import (
    FULL_SCAN_THRESHOLD,
    introspect_collection,
    sample_size_for,
)


def test_sample_size_for_small_collection_scans_fully():
    assert sample_size_for(100) == 100
    assert sample_size_for(FULL_SCAN_THRESHOLD) == FULL_SCAN_THRESHOLD


def test_sample_size_for_large_collection_is_bounded():
    n = sample_size_for(10_000_000)
    assert 1_000 <= n <= 50_000


def test_introspect_collection_infers_types_and_nesting():
    client = mongomock.MongoClient()
    db = client["app"]
    db.orders.insert_many(
        [
            {
                "status": "shipped",
                "items": [{"qty": 2, "price": 19.99}],
                "tagIds": ["t1", "t2"],
            },
            {
                "status": "pending",
                "items": [{"qty": 1, "price": 5.0}],
                "tagIds": ["t1"],
            },
        ]
    )

    schema = introspect_collection(db, "orders")

    assert schema.sampled_count == 2
    assert schema.fields["status"].bson_types == {"string"}
    assert schema.fields["items"].is_array
    assert schema.fields["items"].array_item_kind == "object"
    assert schema.fields["items[].qty"].bson_types == {"int"}
    assert schema.fields["tagIds"].is_array
    assert schema.fields["tagIds"].array_item_kind == "scalar"
    assert schema.fields["tagIds"].array_item_types == {"string"}
    assert schema.top_level_field_names() == {"_id", "status", "items", "tagIds"}


def test_introspect_collection_flags_type_variance():
    client = mongomock.MongoClient()
    db = client["app"]
    db.widgets.insert_many(
        [{"count": 1}, {"count": 2}, {"count": "three"}]
    )
    schema = introspect_collection(db, "widgets")
    assert schema.fields["count"].bson_types == {"int", "string"}


def test_introspect_collection_detects_polymorphism_and_discriminator():
    client = mongomock.MongoClient()
    db = client["app"]
    docs = []
    for _ in range(10):
        docs.append({"type": "card", "last4": "1234"})
    for _ in range(10):
        docs.append({"type": "bank", "routing": "021000021"})
    db.payments.insert_many(docs)

    schema = introspect_collection(db, "payments")

    assert schema.polymorphism_candidate is True
    assert schema.discriminator_field == "type"
