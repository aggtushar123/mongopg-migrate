import mongomock
import pytest

from mongopg_migrate.introspect.postgres import ColumnInfo, PostgresSchema, TableSchema
from mongopg_migrate.mapping.schema import (
    EntityMapping,
    FieldSpec,
    IdStrategy,
    IdStrategyType,
    MappingFile,
)
from mongopg_migrate.migrate.dryrun import run_fast_pass


def col(name: str, data_type: str, nullable: bool = True) -> ColumnInfo:
    return ColumnInfo(name=name, data_type=data_type, is_nullable=nullable, default=None)


def make_pg_schema() -> PostgresSchema:
    return PostgresSchema(
        tables={
            "users": TableSchema(
                name="users",
                columns={"id": col("id", "uuid"), "email": col("email", "text", nullable=False)},
                primary_key=["id"],
            ),
            "orders": TableSchema(
                name="orders",
                columns={
                    "id": col("id", "uuid"),
                    "user_id": col("user_id", "uuid", nullable=False),
                    "status": col("status", "text", nullable=False),
                },
                primary_key=["id"],
            ),
        }
    )


def make_mapping() -> MappingFile:
    return MappingFile(
        entities={
            "users": EntityMapping(
                source="users",
                target="users",
                id_strategy=IdStrategy(type=IdStrategyType.OBJECTID_TO_UUID, source_field="_id"),
                fields={"email": FieldSpec(target="email")},
            ),
            "orders": EntityMapping(
                source="orders",
                target="orders",
                id_strategy=IdStrategy(type=IdStrategyType.OBJECTID_TO_UUID, source_field="_id"),
                fields={
                    "userId": FieldSpec(target="user_id", lookup="users"),
                    "status": FieldSpec(target="status"),
                },
            ),
        }
    )


@pytest.fixture
def mongo_client():
    client = mongomock.MongoClient()
    yield client
    client.close()


def _seed(db, user_id, order_status="shipped", order_user_id=None):
    db.users.insert_one({"_id": user_id, "email": "a@example.com"})
    db.orders.insert_one({"_id": mongomock.ObjectId(), "userId": order_user_id or user_id, "status": order_status})


def test_clean_mapping_produces_no_violations(mongo_client, monkeypatch):
    db = mongo_client["app"]
    user_id = mongomock.ObjectId()
    _seed(db, user_id)
    monkeypatch.setattr("mongopg_migrate.migrate.dryrun.MongoClient", lambda uri: mongo_client)
    mongo_client.get_default_database = lambda: db

    report = run_fast_pass(make_mapping(), "mongodb://fake/app", make_pg_schema())
    assert report.ok, report.violations


def test_lookup_miss_is_flagged(mongo_client, monkeypatch):
    db = mongo_client["app"]
    real_user = mongomock.ObjectId()
    ghost_user = mongomock.ObjectId()  # never inserted into users
    _seed(db, real_user, order_user_id=ghost_user)
    monkeypatch.setattr("mongopg_migrate.migrate.dryrun.MongoClient", lambda uri: mongo_client)
    mongo_client.get_default_database = lambda: db

    report = run_fast_pass(make_mapping(), "mongodb://fake/app", make_pg_schema())
    assert not report.ok
    assert any("lookup miss" in v.message for v in report.violations)


def test_null_into_not_null_column_is_flagged(mongo_client, monkeypatch):
    db = mongo_client["app"]
    user_id = mongomock.ObjectId()
    db.users.insert_one({"_id": user_id, "email": "a@example.com"})
    db.orders.insert_one({"_id": mongomock.ObjectId(), "userId": user_id, "status": None})
    monkeypatch.setattr("mongopg_migrate.migrate.dryrun.MongoClient", lambda uri: mongo_client)
    mongo_client.get_default_database = lambda: db

    report = run_fast_pass(make_mapping(), "mongodb://fake/app", make_pg_schema())
    assert not report.ok
    assert any("NOT NULL" in v.message for v in report.violations)


def test_bad_transform_is_flagged(mongo_client, monkeypatch):
    db = mongo_client["app"]
    user_id = mongomock.ObjectId()
    db.users.insert_one({"_id": user_id, "email": "a@example.com"})
    db.orders.insert_one({"_id": mongomock.ObjectId(), "userId": user_id, "status": "shipped"})
    monkeypatch.setattr("mongopg_migrate.migrate.dryrun.MongoClient", lambda uri: mongo_client)
    mongo_client.get_default_database = lambda: db

    mapping = make_mapping()
    mapping.entities["users"].fields["email"] = FieldSpec(target="email", transform="cast_int")
    report = run_fast_pass(mapping, "mongodb://fake/app", make_pg_schema())
    assert not report.ok
    assert any("email" == v.field for v in report.violations)
