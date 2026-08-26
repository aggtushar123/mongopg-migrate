-- Target Postgres schema for the fixture demo. This is deliberately written
-- as if a team designed it independently of the Mongo shape below (PRD §2's
-- wedge) — column names, casing, and normalization don't mirror the source
-- documents 1:1. Matches the worked example in PRD §12.

CREATE TABLE users (
    id UUID PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL
);

CREATE TABLE products (
    id UUID PRIMARY KEY,
    sku TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL
);

CREATE TABLE tags (
    id UUID PRIMARY KEY,
    label TEXT NOT NULL UNIQUE
);

CREATE TABLE orders (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id),
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE order_items (
    id SERIAL PRIMARY KEY,
    order_id UUID NOT NULL REFERENCES orders(id),
    product_id UUID NOT NULL REFERENCES products(id),
    qty INT NOT NULL,
    price NUMERIC NOT NULL
);

CREATE TABLE order_tags (
    order_id UUID NOT NULL REFERENCES orders(id),
    tag_id UUID NOT NULL REFERENCES tags(id),
    PRIMARY KEY (order_id, tag_id)
);
