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
| Docker image (primary distribution, PRD §8) | ✅ — live-tested: builds clean, every command run from inside the container against the fixture over the compose network |

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

### Docker (primary distribution, per PRD §8)

```bash
docker compose up -d                                          # local Mongo + Postgres fixture
docker build -f docker/Dockerfile -t mongopg-migrate:latest .  # the tool itself

docker run --rm --network mongodbtopostgres_default \
  -e MONGO_URI=mongodb://mongo:27017/app \
  -e POSTGRES_URI=postgresql://postgres:postgres@postgres:5432/app \
  -v "$(pwd)/fixtures/mapping.example.yaml:/app/mapping.yaml:ro" \
  mongopg-migrate:latest migrate /app/mapping.yaml --mode truncate
```

Note the internal Postgres port (`5432`, not the `55432` host-mapped port
from the local `.venv` examples above) and `--network`, pointing the tool's
container at the compose network so `mongo`/`postgres` resolve as hostnames
— both are only relevant when the tool itself runs in a container talking
to other containers; a tool container reaching an external/host database
just uses that database's real connection string, no `--network` needed.
Every command (`introspect`, `propose`, `validate-mapping`, `dry-run`,
`migrate`, `validate`) has been run this way against the fixture above; the
Dockerfile builds successfully on a clean checkout.

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check src tests
```

## License

MIT
