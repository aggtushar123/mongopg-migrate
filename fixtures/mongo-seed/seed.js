// Source Mongo data for the fixture demo. Field names, nesting, and
// looseness are typical of an app-first Mongo schema — deliberately not
// shaped like the independently-designed Postgres target in
// fixtures/postgres-ddl/schema.sql. Matches the worked example in PRD §12.

db = db.getSiblingDB("app");

const userId = ObjectId("64f0b1000000000000000001");
const productId = ObjectId("64e9c3000000000000000001");
const tagIdA = ObjectId("64e0aa000000000000000001");
const tagIdB = ObjectId("64e0ab000000000000000002");

db.users.insertMany([
  { _id: userId, email: "alex@example.com", name: "Alex Rivera" },
]);

db.products.insertMany([
  { _id: productId, sku: "WIDGET-1", name: "Widget" },
]);

db.tags.insertMany([
  { _id: tagIdA, label: "priority" },
  { _id: tagIdB, label: "gift" },
]);

db.orders.insertMany([
  {
    _id: ObjectId("64f1a2000000000000000001"),
    userId: userId,
    status: "shipped",
    createdAt: ISODate("2026-01-04T10:00:00Z"),
    items: [{ productId: productId, qty: 2, price: 19.99 }],
    tagIds: [tagIdA, tagIdB],
  },
  {
    _id: ObjectId("64f1a2000000000000000002"),
    userId: userId,
    status: "pending",
    createdAt: ISODate("2026-01-05T09:30:00Z"),
    items: [{ productId: productId, qty: 1, price: 19.99 }],
    tagIds: [tagIdA],
  },
]);
