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
| `unmapped.jsonb` landing: serialized into one JSON object, written via a real `jsonb` column (`unmapped.jsonb_column`) — was previously a silent no-op indistinguishable from `drop` | ✅ — live-tested: string/float/list/datetime all round-trip correctly through COPY via psycopg's `Jsonb` wrapper |
| Discriminator-filtered mappings (`filter: {field, equals}`, PRD §7 P0 — the last previously-unbuilt P0): closes the loop from detection (already built) to an actual multiple-mappings-per-collection capability | ✅ — live-tested: one polymorphic collection split into two entities/tables, correctly-scoped counts and dry-run checks per filter. `validate-mapping`'s unmapped-field check is filter-aware too (`introspect_entities`, keyed by entity + `entity.mongo_filter()`) — live-confirmed a field only present on the *other* discriminator value no longer shows as a false "no disposition" |
| `unmapped.jsonb` sample-diff value-checking: the jsonb payload is recomputed from the source doc (`migrate.transform.json_safe`, shared with the loader) and compared to what Postgres actually has | ✅ — live-tested: a directly-corrupted jsonb value is caught with the column named; a reordered-but-identical jsonb value (Postgres doesn't preserve key insertion order) correctly does *not* false-positive |
| `int_sequence` batched id reservation: one `SELECT nextval(seq) FROM generate_series(1, N)` round trip per block instead of one `nextval()` per document | ✅ — live-tested against Postgres's own query log: 1200 documents served by exactly 3 `generate_series` round trips (500+500+200, matching the block size), zero individual `nextval()` calls; all 1200 ids unique and correct |
| Cross-run lookups (`external_entities`, PRD §12's own worked example): `lookup:` can name an entity migrated in an *earlier*, separate run, resolved via `_mongopg.id_map` instead of requiring every referenced entity to be declared in the same mapping file | ✅ — live-tested: two independent `migrate` runs against two separate mapping files, FK correctly resolved across them |
| `append`/`upsert` resuming a `done` entity: previously frozen after first completion — new documents inserted since required manually deleting the checkpoint row | ✅ — live-tested: new document picked up automatically on next run, "nothing new" case still reports cleanly |
| Cross-**database** lookups (`external_databases`, a microservices split — N target Postgres databases from one Mongo source): a `lookup:` entity can live in a genuinely different database, not just the same one from an earlier run. Maps entity name -> an env var holding that database's connection string (never a raw credential in the checked-in mapping file) | ✅ — live-tested with two actually-separate Postgres databases (not schemas): migrated `hospitals` into one, then `bookings` (referencing it via `lookup:`) into a completely different one, over a live cross-database connection — FK resolved correctly, `dry-run`/`validate` both cross-database-aware too. One real bug caught and fixed by this same live test: Layer B's disposable internal-schema name was leaking into the external entity's lookup, which must always use its own database's real `_mongopg` schema regardless of what this run calls its own — now covered by both the live test and a fast unit regression test |
| `enum:`/`split:` transforms — the last two items in the PRD's P0 transform DSL list, both prompted by a real user hitting exactly these gaps in a Prisma→Postgres migration | ✅ — `enum:<json mapping>` remaps a value through an explicit table (`"*"` wildcard for a fallback, otherwise a loud error on an unlisted value); `split:<delimiter>` turns a delimited string into a list for a Postgres ARRAY column. Live-tested, both directions. Also discovered live: a Mongo array field mapped **without any transform at all** already lands correctly on a Postgres ARRAY column (psycopg's COPY path adapts Python lists automatically) — not a gap that needed closing, just needed confirming and documenting |
| CLI: `introspect`, `propose`, `validate-mapping`, `migrate`, `dry-run`, `validate` | ✅ |
| Docker image (primary distribution, PRD §8) | ✅ — live-tested: builds clean, every command run from inside the container against the fixture over the compose network |
| LLM-assisted mapping suggestions (`propose --llm`, PRD §7 P1/§8): pluggable `LLMClient` seam, schema-metadata-only payload, never trusts a suggestion blindly | ✅ — provider-agnostic via `--llm-provider`: `anthropic` (Anthropic API) or `openai-compatible` (plain HTTP to any server speaking the OpenAI chat-completions contract — OpenAI, Azure OpenAI, Ollama, vLLM, LM Studio, llama.cpp server, ..., zero added dependency). `openai-compatible` is live-tested against a real local HTTP server (12 tests, genuine socket round-trips). `anthropic` is **not** live-verified end-to-end (no credentials in this dev environment): unit-tested against a fake client, and one real network call with a deliberately invalid key confirmed a genuine 401 from Anthropic's servers, not a client-side SDK-usage error |

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

# Optional: ask an LLM about fields propose couldn't confidently map on its own
# (e.g. a rename like users.name -> display_name). Off by default; only field
# names/types/shapes are sent, never row data. Provider-agnostic:

# ...via the Anthropic API — requires: pip install "mongopg-migrate[llm]"
#    and export ANTHROPIC_API_KEY=... (or `ant auth login`)
mongopg-migrate propose \
  --mongo-uri mongodb://localhost:27017/app \
  --postgres-uri postgresql://postgres:postgres@localhost:55432/app \
  -o mapping.yaml --llm

# ...or any OpenAI-compatible server — OpenAI itself, Azure OpenAI, or a local
#    runtime (Ollama, vLLM, LM Studio, llama.cpp server, ...). No extra
#    package needed. Example against a local Ollama running llama3:
mongopg-migrate propose \
  --mongo-uri mongodb://localhost:27017/app \
  --postgres-uri postgresql://postgres:postgres@localhost:55432/app \
  -o mapping.yaml --llm \
  --llm-provider openai-compatible \
  --llm-base-url http://localhost:11434/v1 \
  --llm-model llama3

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

### Splitting one Mongo source across N Postgres databases (e.g. microservices)

Each `mongopg-migrate` run targets exactly one `--postgres-uri` — the tool
doesn't orchestrate multiple runs for you. For a source fanned out across
several service databases, that's **N independent runs**, one mapping file
per target database, each declaring only the entities whose tables live
there; run them in dependency order and wire the ordering/env-var-per-run
plumbing with a script or Makefile outside the tool. What the tool *does*
handle is the hard part inside that shape — a `lookup:` whose target entity
was migrated into a genuinely different database:

```yaml
# booking.yaml — targets the booking-service database
external_entities: [hospitals]
external_databases:
  hospitals: HOSPITAL_POSTGRES_URI   # env var name, never a raw credential here

entities:
  bookings:
    source: bookings
    target: bookings
    id_strategy: { type: objectid_to_uuid, source_field: _id, target_field: id }
    fields:
      hospitalId: { target: hospital_id, lookup: hospitals }
      patientName: patient_name
```

```bash
# hospitals migrated first, into its own database
mongopg-migrate migrate hospital.yaml \
  --mongo-uri "$MONGO_URI" --postgres-uri "$HOSPITAL_POSTGRES_URI" --mode truncate

# bookings resolves hospitalId over a live connection to that other database
export HOSPITAL_POSTGRES_URI="postgresql://.../hospital_db"
mongopg-migrate migrate booking.yaml \
  --mongo-uri "$MONGO_URI" --postgres-uri "$BOOKING_POSTGRES_URI" --mode truncate
```

`dry-run` and `validate` are cross-database-aware the same way. Live-tested
against two genuinely separate Postgres databases (not just schemas) — see
the Status table above.

### Transform DSL

`FieldSpec.transform` supports `cast_int`/`cast_float`/`cast_text`/`cast_bool`/`cast_timestamptz`,
`default:<literal>`, `json_extract:<path>` (informational — the dotted
field key already resolves this), `enum:<json mapping>`, and
`split:<delimiter>`:

```yaml
fields:
  statusCode: { target: status, transform: 'enum:{"1": "active", "2": "inactive", "*": "unknown"}' }
  tagString: { target: tags, transform: "split:," }        # "vip,new" -> {vip,new}
  allergies: allergies                                      # Mongo array -> Postgres ARRAY column,
                                                              # no transform needed at all
```

`enum:` is the one most Prisma/ORM migrations end up needing — a stored
enum whose labels don't match the target column's labels verbatim.

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
