# mongopg-migrate

[![CI](https://github.com/aggtushar123/mongopg-migrate/actions/workflows/ci.yml/badge.svg)](https://github.com/aggtushar123/mongopg-migrate/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

Map MongoDB collections onto an **existing, independently designed**
PostgreSQL schema, and run a validated, repeatable data migration — without
hand-writing a transform script.

This is not a tool that generates a Postgres schema from your Mongo shape.
You bring the target DDL (greenfield design, a rewrite, or ORM models you
already built); this tool figures out — and has you confirm — how your
Mongo documents map onto it. See [`PRD-mongo-postgres-migration-tool.md`](./PRD-mongo-postgres-migration-tool.md)
for the full product spec; module docstrings in `src/mongopg_migrate/`
reference PRD section numbers throughout.

## Status

Early, pre-alpha. Implemented so far:

| Piece | Status |
|---|---|
| Mongo introspection (sampling, type/variance inference, polymorphism detection) | ✅ |
| Postgres introspection (schema, FKs, FK-derived load order) | ✅ |
| Mapping-file format + structural/unmapped-field validation | ✅ |
| Rule-based candidate mapping proposer | ✅ |
| Batch loader: entity-ordered COPY, `_mongopg.id_map`, per-batch checkpoint/resume, `truncate`/`append` | ✅ — live-tested including a real SIGKILL mid-run + resume (4002 docs, zero dupes/orphans) |
| Dry-run: Layer A (in-memory type/null/lookup checks) + Layer B (real COPY+FK load into a disposable schema clone) | ✅ — live-tested: catches a real lookup miss before any write, leaves zero artifacts on success or failure |
| Post-migration validation: count diff (incl. explode/junction tables) + hashed-field sample diff | ✅ — live-tested: catches a real corrupted value with the exact field + row identified, clean data passes |
| `--mode upsert`: staging table + `ON CONFLICT DO UPDATE` for the main entity and `junction` tables; `explode` children always plain-insert (no natural conflict key) | ✅ |
| CLI: `introspect`, `propose`, `validate-mapping`, `migrate`, `dry-run`, `validate` | ✅ |

## Try it

```bash
docker compose up -d          # local Mongo + Postgres, seeded from fixtures/
pip install -e ".[dev]"

mongopg-migrate introspect \
  --mongo-uri mongodb://localhost:27017/app \
  --postgres-uri postgresql://postgres:postgres@localhost:55432/app

mongopg-migrate propose \
  --mongo-uri mongodb://localhost:27017/app \
  --postgres-uri postgresql://postgres:postgres@localhost:55432/app \
  -o mapping.yaml

mongopg-migrate validate-mapping mapping.yaml \
  --mongo-uri mongodb://localhost:27017/app

mongopg-migrate dry-run fixtures/mapping.example.yaml \
  --mongo-uri mongodb://localhost:27017/app \
  --postgres-uri postgresql://postgres:postgres@localhost:55432/app

mongopg-migrate migrate fixtures/mapping.example.yaml \
  --mongo-uri mongodb://localhost:27017/app \
  --postgres-uri postgresql://postgres:postgres@localhost:55432/app \
  --mode truncate

mongopg-migrate validate fixtures/mapping.example.yaml \
  --mongo-uri mongodb://localhost:27017/app \
  --postgres-uri postgresql://postgres:postgres@localhost:55432/app
```

`fixtures/mapping.example.yaml` is a hand-written, fully-worked mapping for
the seeded fixture data (orders → orders/order_items/order_tags) matching
the PRD §12 example — a reference for what `propose` should get most of the
way to on its own.

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check src tests
```

## License

MIT
