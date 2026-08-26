from mongopg_migrate.introspect.mongo import CollectionSchema, FieldStats
from mongopg_migrate.introspect.postgres import ColumnInfo, ForeignKey, PostgresSchema, TableSchema
from mongopg_migrate.mapping.propose import propose_mapping
from mongopg_migrate.mapping.schema import IdStrategyType


def fs(path: str, types: set[str], *, is_array=False, array_item_kind=None, array_item_types=None) -> FieldStats:
    return FieldStats(
        path=path,
        present_count=2,
        null_count=0,
        bson_types=set(types),
        is_array=is_array,
        array_item_kind=array_item_kind,
        array_item_types=set(array_item_types or []),
    )


def collection(name: str, fields: dict[str, FieldStats]) -> CollectionSchema:
    return CollectionSchema(
        name=name,
        document_count=2,
        sampled_count=2,
        fields=fields,
        shape_signature_counts={},
        polymorphism_candidate=False,
        discriminator_field=None,
    )


def col(name: str, data_type: str, nullable: bool = True) -> ColumnInfo:
    return ColumnInfo(name=name, data_type=data_type, is_nullable=nullable, default=None)


def _fk(column: str, references_table: str, references_column: str = "id") -> ForeignKey:
    return ForeignKey(
        constraint_name=f"fk_{column}",
        column=column,
        references_table=references_table,
        references_column=references_column,
        is_deferrable=False,
    )


def build_fixture():
    mongo_schemas = {
        "users": collection(
            "users",
            {
                "_id": fs("_id", {"objectid"}),
                "email": fs("email", {"string"}),
                "name": fs("name", {"string"}),
            },
        ),
        "products": collection(
            "products",
            {
                "_id": fs("_id", {"objectid"}),
                "sku": fs("sku", {"string"}),
                "name": fs("name", {"string"}),
            },
        ),
        "tags": collection(
            "tags",
            {"_id": fs("_id", {"objectid"}), "label": fs("label", {"string"})},
        ),
        "orders": collection(
            "orders",
            {
                "_id": fs("_id", {"objectid"}),
                "userId": fs("userId", {"objectid"}),
                "status": fs("status", {"string"}),
                "createdAt": fs("createdAt", {"date"}),
                "items": fs("items", {"array"}, is_array=True, array_item_kind="object"),
                "items[].productId": fs("items[].productId", {"objectid"}),
                "items[].qty": fs("items[].qty", {"int"}),
                "items[].price": fs("items[].price", {"double"}),
                "tagIds": fs(
                    "tagIds", {"array"}, is_array=True, array_item_kind="scalar", array_item_types={"objectid"}
                ),
            },
        ),
    }

    pg_schema = PostgresSchema(
        tables={
            "users": TableSchema(
                name="users",
                columns={"id": col("id", "uuid"), "email": col("email", "text"), "display_name": col("display_name", "text")},
                primary_key=["id"],
            ),
            "products": TableSchema(
                name="products",
                columns={"id": col("id", "uuid"), "sku": col("sku", "text"), "name": col("name", "text")},
                primary_key=["id"],
            ),
            "tags": TableSchema(
                name="tags",
                columns={"id": col("id", "uuid"), "label": col("label", "text")},
                primary_key=["id"],
            ),
            "orders": TableSchema(
                name="orders",
                columns={
                    "id": col("id", "uuid"),
                    "user_id": col("user_id", "uuid"),
                    "status": col("status", "text"),
                    "created_at": col("created_at", "timestamptz"),
                },
                primary_key=["id"],
                foreign_keys=[_fk("user_id", "users")],
            ),
            "order_items": TableSchema(
                name="order_items",
                columns={
                    "id": col("id", "integer"),
                    "order_id": col("order_id", "uuid"),
                    "product_id": col("product_id", "uuid"),
                    "qty": col("qty", "integer"),
                    "price": col("price", "numeric"),
                },
                primary_key=["id"],
                foreign_keys=[_fk("order_id", "orders"), _fk("product_id", "products")],
            ),
            "order_tags": TableSchema(
                name="order_tags",
                columns={"order_id": col("order_id", "uuid"), "tag_id": col("tag_id", "uuid")},
                primary_key=["order_id", "tag_id"],
                foreign_keys=[_fk("order_id", "orders"), _fk("tag_id", "tags")],
            ),
        }
    )
    return mongo_schemas, pg_schema


def test_propose_mapping_matches_the_prd_worked_example():
    mongo_schemas, pg_schema = build_fixture()
    mapping, _issues = propose_mapping(mongo_schemas, pg_schema)

    assert set(mapping.entities) == {"users", "products", "tags", "orders"}

    orders = mapping.entities["orders"]
    assert orders.target == "orders"
    assert orders.id_strategy.type == IdStrategyType.OBJECTID_TO_UUID
    assert orders.id_strategy.target_field == "id"

    assert orders.fields["userId"].target == "user_id"
    assert orders.fields["userId"].lookup == "users"
    assert orders.fields["status"].target == "status"

    explode = orders.explode["items"]
    assert explode.target == "order_items"
    assert explode.parent_fk.target_field == "order_id"
    assert explode.parent_fk.references == "orders.id"
    assert explode.fields["productId"].target == "product_id"
    assert explode.fields["productId"].lookup == "products"
    assert explode.fields["qty"].target == "qty"

    junction = orders.junction["tagIds"]
    assert junction.target == "order_tags"
    assert junction.parent_fk.target_field == "order_id"
    assert junction.child_fk.target_field == "tag_id"
    assert junction.child_fk.references == "tags.id"
    assert junction.child_fk.lookup == "tags"

    # Everything on the orders collection got a disposition — nothing silently unmapped.
    assert not orders.unmapped.drop
    assert not orders.unmapped.jsonb


def test_propose_mapping_flags_type_variance_instead_of_guessing():
    mongo_schemas, pg_schema = build_fixture()
    mongo_schemas["users"].fields["email"] = fs("email", {"string", "int"})  # type variance
    mapping, issues = propose_mapping(mongo_schemas, pg_schema)

    users = mapping.entities["users"]
    assert "email" not in users.fields
    assert "email" in users.unmapped.drop or "email" in users.unmapped.jsonb
    assert any("type variance" in i.message for i in issues if i.field == "email")


def test_propose_mapping_flags_polymorphism_candidate():
    mongo_schemas, pg_schema = build_fixture()
    mongo_schemas["orders"].polymorphism_candidate = True
    mongo_schemas["orders"].discriminator_field = "status"
    _, issues = propose_mapping(mongo_schemas, pg_schema)
    assert any("shape variance" in i.message for i in issues if i.entity == "orders")
