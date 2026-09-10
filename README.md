# mongopg-migrate

[![CI](https://github.com/aggtushar123/mongopg-migrate/actions/workflows/ci.yml/badge.svg)](https://github.com/aggtushar123/mongopg-migrate/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/aggtushar123/mongopg-migrate/blob/main/LICENSE)

Map MongoDB collections onto an **existing, independently designed**
PostgreSQL schema, and run a validated, repeatable data migration — without
hand-writing a transform script.

This is not a tool that generates a Postgres schema from your Mongo shape.
You bring the target DDL (greenfield design, a rewrite, or ORM models you
already built); this tool figures out — and has you confirm — how your
Mongo documents map onto it. See [`PRD-mongo-postgres-migration-tool.md`](https://github.com/aggtushar123/mongopg-migrate/blob/main/PRD-mongo-postgres-migration-tool.md)
for the full product spec; module docstrings in `src/mongopg_migrate/`
reference PRD section numbers throughout.

## Status

**Alpha.** The core loop — `introspect` → `propose` → `validate-mapping` →
`dry-run` → `migrate` → `validate` — works end to end and is exercised on
every push against real MongoDB and PostgreSQL containers: over 340 unit
tests and 42 live integration tests.

It has been used for one large real migration, which is where most of its
sharp edges came from. It has not been used by anyone else yet, so
treat unfamiliar shapes as unproven: run `dry-run` against a clone of your
target schema before you trust it with anything you cannot rebuild.

| Piece | Proof |
|---|---|
| Mongo introspection — sampling, type-variance and polymorphism detection | `test_mongo_introspect.py` |
| Postgres introspection — schema, FKs, FK-derived load order | `test_postgres_load_order.py` + live |
| Mapping file — format, structural + unmapped-field validation, collection coverage | `test_mapping_schema.py` (17) |
| Candidate mapping proposer — rule-based, with optional `--llm` | `test_propose.py` (3), `test_llm_propose.py` (15), `test_llm_client_openai_compatible.py` (12) |
| Loader — entity-ordered COPY, `_mongopg.id_map`, per-batch checkpoint and resume | live: SIGKILL-mid-run + resume, full round trip |
| Document shapes — nested `explode:`, `junction:`, `unpivot:`, discriminator `filter:` | `test_nested_explode.py` (15), `test_unpivot.py` (13), `test_explode_unmapped.py` (12) |
| Transform DSL — casts, `enum:`, `split:`, `truncate:`, `trim`, `\|` pipelines | `test_transform.py` (25), `test_transform_pipeline.py` (37) |
| `lookup:` with `on_missing` policies; cross-run and cross-**database** | `test_on_missing.py` (37), `test_cross_database_lookups.py` (11) |
| Load ordering, including `depends_on:` for edges the graph cannot infer | `test_depends_on.py` (14), `test_entity_load_order.py` |
| Dry run — Layer A in-memory checks + Layer B real COPY into a disposable clone | `test_dryrun_fast_pass.py` + live |
| `validate` — count diff plus hashed-field sample diff | `test_validate_canonicalize.py` (12) + live |
| `--pg-schema` honoured on read **and** write | `test_target_schema.py` (11) + live (4) |
| `mongopg-fanin` — the fan-in (N docs → 1 row) helper | `test_fanin_reshape.py` (21); `$merge` path hand-verified |
| `--mode upsert` — staging table + `ON CONFLICT` | `test_load_upsert.py` (4), `integration/test_upsert_live.py` (6) |
| `--internal-schema` and `--uuid-namespace` escape hatches | `test_connerrors.py` (19), `integration/test_escape_hatches_live.py` (10) |
| `validate` count-diff correctness (shared targets, skip_row rows, skipped children, numeric scale) | `test_validate_countdiff.py` (28) |
| Operational safety — `truncate` confirmation, `--only` subset, progress output | `integration/test_operational_live.py` (12) |
| Docker image | ⚠️ hand-verified; built multi-arch by `release.yml`, but no test asserts it behaves |
| `append`/`upsert` resuming an entity already marked `done` | `integration/test_resume_done_entity_live.py` (8) |

Each row links to the tests that back it, and only to tests that exist. The
detailed history behind these — the bugs, why each fix is shaped the way it
is, and which claims rest on a hand-run session rather than a test — is in
[`docs/engineering-log.md`](https://github.com/aggtushar123/mongopg-migrate/blob/main/docs/engineering-log.md), where every entry is
labelled with the evidence that actually supports it.

## Install

```bash
pip install mongopg-migrate
```

Two commands are installed: `mongopg-migrate` (the migration tool) and
`mongopg-fanin` (a Mongo-side reshape helper for the fan-in case — see
[`docs/fanin-reshape.md`](https://github.com/aggtushar123/mongopg-migrate/blob/main/docs/fanin-reshape.md)).

Or as a container, with no Python install at all:

```bash
docker pull ghcr.io/aggtushar123/mongopg-migrate:latest
```

Python 3.11+. For working on the tool itself, see [Development](https://github.com/aggtushar123/mongopg-migrate/blob/main/README.md#development).

## Try it

```bash
git clone https://github.com/aggtushar123/mongopg-migrate && cd mongopg-migrate
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

### Targeting a schema other than `public`

Every command takes `--pg-schema` (default `public`). It selects the schema
that is introspected **and** the one the load writes into, so pass the same
value to every command in a run:

```bash
mongopg-migrate dry-run  mapping.yaml --pg-schema app ...
mongopg-migrate migrate  mapping.yaml --pg-schema app ...
mongopg-migrate validate mapping.yaml --pg-schema app ...
```

Two notes:

- The tool sets `search_path` explicitly for the duration of the run, so the
  connecting role's own `search_path` does not affect where data lands.
  Before v0.1.1 it did: `--pg-schema` chose what was *read* while writes
  followed the role's default, which silently loaded a non-`public` target
  into the wrong schema. If you ran an earlier version against a named
  schema, check where the rows actually are.
- `_mongopg` (the internal `id_map`/checkpoint schema) is always its own
  schema and is not affected by `--pg-schema`.

A schema that does not exist is now reported as such, with the available
schemas listed, rather than looking like a target with no tables.

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

`FieldSpec.transform` supports:

| Transform | Does |
|---|---|
| `cast_int` `cast_float` `cast_text` `cast_bool` `cast_timestamptz` | Type coercion. All reject a list/dict loudly rather than coercing it. |
| `default:<literal>` | Used only when the resolved value is `None`. |
| `enum:<json mapping>` | Remaps a value through an explicit table. `"*"` is a fallback; an unlisted value without one is an error. |
| `split:<delimiter>` | Delimited string → list, for a Postgres `ARRAY` column. |
| `truncate:<n>` | Caps a string at `n` characters. |
| `trim` | Strips leading/trailing whitespace. |
| `json_extract:<path>` | Informational — the dotted field key already resolves this. |

```yaml
fields:
  statusCode: { target: status, transform: 'enum:{"1": "active", "2": "inactive", "*": "unknown"}' }
  tagString: { target: tags, transform: "split:," }        # "vip,new" -> {vip,new}
  allergies: allergies                                      # Mongo array -> Postgres ARRAY column,
                                                              # no transform needed at all
```

`enum:` is the one most Prisma/ORM migrations end up needing — a stored
enum whose labels don't match the target column's labels verbatim.

#### Pipelines

Steps separated by `|` run left to right, because one field can need two
unrelated fixes and `transform:` has only one slot:

```yaml
fields:
  # absent on some documents, over-long on others, and the column is NOT NULL
  hlNumber: { target: reference_code, transform: "default:UNKNOWN|truncate:32" }
  # pad-stripped, then parsed
  qtyText:  { target: qty, transform: "trim|cast_int" }
```

Three things worth knowing:

- **`truncate:` is never automatic.** A transform that silently trimmed every
  over-long value to fit would be the quiet corruption this tool exists to
  prevent — counts would match while values were subtly wrong. Writing
  `truncate:32` makes the loss a declared decision at one named field, visible
  in review. `validate` applies the same transform to the source side, so a
  truncated value is not reported as a mismatch; the mapping file is the
  record of what was given up.
- **`trim` removes padding, never content.** Internal whitespace is left
  alone: collapsing `Liberty  General` is a normalisation decision that
  belongs in a lookup table, not a field transform.
- **A `default:` literal is not put through the rest of the pipeline.** It is
  authored to be the final value, so `default:LONGVALUE|truncate:3` yields
  `LONGVALUE`, not `LON` — a default longer than the column will still fail
  the load. Write a default that already fits.

A `|` inside an `enum:` JSON object is not a step separator, so
`enum:{"A|B": "both"}` works. The one case the syntax cannot express is
`split:` on a literal pipe *combined with other steps* — `split:|` alone is
fine, `trim|split:|` is ambiguous and says so.

### Running it on something you care about

- **`--mode truncate` asks first.** It lists the tables it will empty, the
  schema, and the server (password masked), and defaults to no. Pass `--yes`
  for automation — scripts that ran `--mode truncate` non-interactively before
  v0.1.2 need it added. `append` and `upsert` are not gated; prompting on
  non-destructive modes only teaches people to hit `y` without reading.
- **`--only <entity>`** (repeatable) runs part of a mapping, for a phased
  cutover. Load order still comes from the full graph, so a selected entity
  keeps its place. `truncate` is scoped to the selection too — it will not
  empty tables this run is not going to reload. An entity a run depends on
  must already be loaded; a `lookup:` into one that has never loaded is
  refused, which is what makes running a subset safe rather than merely
  possible.
- **Progress is reported** while an entity loads, at most one line every few
  seconds, so a long run over a slow link is distinguishable from a hung one.
- **`--idmap-prefetch-max`** (default 2,000,000) caps how many id_map rows are
  held in memory per referenced entity. The snapshot is roughly 200 bytes a
  row, so an unbounded prefetch is a memory ceiling waiting for a big enough
  source — about 12 GB for a 50M-row entity, before the first document is
  processed. Above the cap the entity is looked up per row instead, with a
  bounded cache in front; the run says so and names the flag. Slower, but it
  cannot exhaust memory.

### Fitting an existing environment

Two escape hatches for targets that cannot take the defaults. Both must be
passed identically to every command in a migration.

**`--internal-schema`** (default `_mongopg`) renames the schema holding this
tool's own `id_map` and `load_checkpoint` tables — for a target whose owner
will not grant a schema by that name. `validate` run against the wrong one
says so rather than reporting a clean pass.

**`--uuid-namespace`** overrides the uuid5 namespace behind the
`objectid_to_uuid` id strategy. Override it **only** to reproduce ids minted
by an earlier cutover under a namespace of its own; otherwise every foreign
key to that previously-migrated data would point somewhere new.

```bash
mongopg-migrate migrate mapping.yaml \
  --internal-schema app_migration \
  --uuid-namespace 11111111-2222-4333-8444-555555555555 ...
```

Changing the namespace partway through a migration is refused: the same
document would resolve to a different UUID, so a resume would insert a second
copy of every row instead of continuing. Before resuming, the loader compares
what the id_map already stores against what the current namespace derives,
and stops if they disagree.

### Re-running a migration

`--mode truncate` empties the mapped tables and starts over. `append` and
`upsert` keep what is there and continue from a per-entity checkpoint. Three
things about the second case are worth knowing before you rely on it for a
cutover:

- **A completed entity resumes at `_id > last_source_id`.** New documents are
  picked up on the next run; the checkpoint advances; a run with nothing new
  reports "already fully loaded", writes nothing, and exits 0.
- **A MODIFIED document is not picked up.** Editing a document does not change
  its `_id`, so a re-run skips straight past it and reports success having
  looked at nothing. This is not a failed update — the document is never read.
  To re-pass over data that already loaded, clear the entity's checkpoint row
  in `_mongopg.load_checkpoint`; `upsert` then rewrites the existing rows in
  place.
- **Re-processing a document duplicates its `explode` children.** Child rows
  have no natural conflict key, so they are always plain-inserted, while the
  parent row is upserted. `validate`'s count diff catches this (the child
  table reports more rows than the source array holds), but the loader itself
  will not complain. `junction` and `unpivot` rows do have natural keys and
  are not affected.

Run `validate` after any re-run, not just the first load.

### Load order

`migrate` derives the order from the mapping: every `lookup:` is an edge, and
referenced entities load first. A cycle is an error rather than a guess.

`depends_on:` declares an edge the graph cannot infer — the case being a
foreign key whose value is resolved through hand-seeded `external_entities`
id_map rows, so the entity never names its parent in a `lookup:` at all and
would otherwise float to the front and `COPY` before the parent exists:

```yaml
entities:
  hospital_documents:
    source: hospitalDocuments
    target: hospital_documents
    depends_on: [hospitals]     # FK is to hospitals; no lookup: names it
```

It orders entities **within one run**, so it must name an entity in the same
mapping file — an `external_entities` name is rejected, since that data is
already loaded. A name that is not in the file is an error too: at ordering
time an unknown name is trivially satisfied, so a typo would silently restore
the exact bug `depends_on` exists to fix.

### Docker (primary distribution, per PRD §8)

```bash
docker pull ghcr.io/aggtushar123/mongopg-migrate:latest
# ...or build it yourself:
docker build -f docker/Dockerfile -t mongopg-migrate:latest .

docker compose up -d                # local Mongo + Postgres fixture

docker run --rm --network mongopg-migrate_default \
  -e MONGO_URI=mongodb://mongo:27017/app \
  -e POSTGRES_URI=postgresql://postgres:postgres@postgres:5432/app \
  -v "$(pwd)/fixtures/mapping.example.yaml:/work/mapping.yaml:ro" \
  ghcr.io/aggtushar123/mongopg-migrate:latest migrate /work/mapping.yaml --mode truncate
```

The fan-in helper is the image's second entry point:

```bash
docker run --rm --entrypoint mongopg-fanin \
  ghcr.io/aggtushar123/mongopg-migrate:latest --help
```

The image runs as a non-root user and works in `/work`, so mount your mapping
file there. The network is `mongopg-migrate_default` on every machine because
`docker-compose.yml` pins the Compose project name — before v0.1.1 it was
derived from the checkout directory, so the name documented here was wrong for
anyone who had cloned into a differently-named folder.

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

`pytest` alone (no env vars) runs the unit-test suite only — fast, no network, exactly what CI's matrixed `test` job runs. `tests/integration/` (real Mongo + real Postgres, including a genuine SIGKILL-and-resume) runs separately, in its own CI job, and is skipped cleanly by the command above unless `MONGO_URI`/`POSTGRES_URI` are set:

```bash
docker compose up -d
MONGO_URI=mongodb://localhost:27017/app \
POSTGRES_URI=postgresql://postgres:postgres@localhost:55432/app \
pytest -q tests/integration
```

## License

MIT
