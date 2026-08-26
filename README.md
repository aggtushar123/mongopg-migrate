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
| Post-migration validation: count diff (incl. explode/junction/unpivot tables) + hashed-field sample diff | ✅ — live-tested: catches a real corrupted value with the exact field + row identified, clean data passes |
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
| Explode/junction array-shape safety (PRD §7 P0): `doc.get(field) or []` silently iterated a truthy scalar string character-by-character (e.g. a plain `department: "CARDIOLOGY"` mapped via `junction:` produced 10 silent one-letter rows, no error) — found by testing the loader against a real mapping. Loader now hard-fails with the field name/type/value and a fix suggestion (`LoadError`); dry-run Layer A reports it as a violation instead of writing anything | ✅ — 16 regression tests, including a direct reproduction of the character-by-character iteration |
| `cast_bool`/`cast_text` array safety: found live re-verifying a transcript's "arrays of plain scalars" note (already resolved — see the `enum:`/`split:` row above — but re-checking it directly, rather than trusting the earlier note, surfaced this) — `bool(some_list)` is `True` for any non-empty list and `False` for an empty one (Python truthiness, not a real cast), and `str(some_list)` silently lands the Python repr `'[1, 2, 3]'` in a text column; neither raised. `cast_int`/`cast_float` already rejected a list on their own (`int()`/`float()` naturally do); `enum:`/`split:` already raised loudly too — only `cast_bool`/`cast_text` were quietly wrong, same footgun class as the character-by-character scalar-iteration bug above | ✅ — 5 regression tests; live-tested: a `flags: ["a", "b"]` field mapped with `transform: cast_bool` now fails dry-run with a clear message instead of silently landing `is_active = true` |
| `unpivot:` construct (PRD §7 P0, worked example §12.2): N differently-named top-level scalar fields (e.g. `pfAmount`/`payToHospital`/`finalBill`) → N rows in an existing child table, each carrying a literal `code` — the EAV/pivot-normalization shape neither `explode` (one array, repeated shape) nor `junction` (one array of scalar FKs) can express. Natural key `(parent_fk, code)` makes `--mode upsert` genuinely meaningful (unlike `explode` children) | ✅ — live-tested: mixed presence/absence/explicit-null across 3 documents produced exactly the expected 5 rows (`skip_null` respected), re-running in upsert mode updated one row's value in place with zero duplicates; count-diff (`bookings_test.amounts: mongo=5 postgres=5`) and dry-run Layer A (transform errors + NOT NULL, respecting `skip_null`) both cover it |
| Nested `explode:` (PRD §7 P0, worked example §12.1) — a second embedded array one level down (e.g. `hospitalDetails.facilities[].categoryParts[]` → a `HospitalFacility` row per facility, each with its own `FacilityCategoryPart` child rows). A middle level's own id is resolved *before* its row is COPYed (`resolve_new_id`, previously called only for the top-level entity) so it can be threaded down as the nested level's `parent_fk` — `explode.id_strategy` was a validated-but-unread field before this; `serial` is now rejected on any level that has nested children, since a SERIAL value isn't known until after INSERT and COPY has no RETURNING | ✅ — live-tested: 2 hospitals / 3 facilities / 3 category-parts loaded with correct FK threading at both levels (verified by joining all three tables back together), a facility with no `categoryParts` at all produced zero grandchild rows, a scalar-where-array-expected mistake was caught by dry-run *and* hard-failed migrate with a clean rollback + resume rather than writing anything wrong, count-diff correctly reports both nesting levels (`hospitals_test.facilities.categoryParts: mongo=3 postgres=3`) |
| Fan-in reshape helper (PRD §4 non-goal — deliberately **not** part of the mapping DSL): `scripts/fanin_reshape.py`, a standalone script outside `src/mongopg_migrate`, for the "N Mongo documents → 1 target row" case (e.g. latest `KycVerificationStep` per booking) no mapping construct can express. Wraps the standard `$match → $sort → $group → $replaceRoot → $out\|$merge` pattern with dry-run preview, a confirmation gate, and clean errors — then you point `mongopg-migrate` at the resulting already-1:1 derived collection like any other. See `scripts/README.md` | ✅ — live-tested: 6→3 doc reduction with correct latest-status-wins-per-group and `--pick-order asc` (earliest-wins) both confirmed, `--mode merge` verified to leave an unrelated pre-existing document untouched (vs. `--mode out` replacing the whole collection), the missing-unique-index failure `$merge` requires surfaces a clear actionable error instead of a raw traceback, the derived collection round-tripped through the full `validate-mapping`/`dry-run`/`migrate`/`validate` pipeline with zero special-casing |
| `on_missing: error\|null\|skip_row` (PRD §7 P0, worked example §12.3) — policy for a *dangling* `lookup:` (source value present, but nothing resolves it, e.g. the referenced document was deleted). Previously an unconditional hard-fail with no way to say "I know about this." `null` writes NULL (still fails against a genuinely NOT NULL column — a policy can't rescue a real schema mismatch); `skip_row` drops the row the field's value belongs to — the whole document for a top-level field, one array item for `explode`, one join row for `junction` (`junction` only accepts `error`/`skip_row`, never `null` — `child_fk` is half the row's own identity). Every occurrence counted and reported at migrate, dry-run (as a non-blocking info notice), and independently re-derived at `validate` (count-diff reconciles the known `skip_row` reduction; sample-diff correctly matches a `null`-rescued row instead of false-flagging every one) | ✅ — live-tested against a real dangling-reference scenario (a `KycVerificationStep` referencing a deleted `McmUser`, plus a `junction` tag reference to a deleted tag): default `error` still hard-fails identically to before; `null` correctly nulled the one dangling row while the other two resolved normally, with `validate` showing zero false-positive mismatches (the exact bug this fix targets) and independently re-deriving the same dangling count; `skip_row` correctly dropped only the affected document (junction: only the affected join row, parent order row intact) while advancing the checkpoint past it (confirmed no infinite-retry on re-run) — a real, live-caught bug fixed in the same pass: `validate`'s count-diff didn't reconcile `skip_row`'s deliberate reduction and reported a clean migration as `Validation FAILED`; also fixed live: bare `on_missing: null` in YAML parses to Python `None`, not the string `"null"` — now coerced rather than rejected with a confusing enum error |
| Duplicate-key safety in the mapping file: `yaml.safe_load`'s default silent last-one-wins for a repeated key (two `fields:` entries for the same source field, two entities with the same name, ...) now raises loudly at `load_mapping_file` instead — found live, writing a mapping that mapped one source field twice (once via `lookup:`, once as a raw passthrough copy): the first entry vanished with zero warning. Same footgun class as the scalar-iteration and bare-`null` bugs above | ✅ — a custom `yaml.SafeLoader` subclass overrides `construct_mapping` to detect the collision before pydantic ever sees the (already-collapsed) dict; live-confirmed both that the duplicate case now raises with a clear message and that the checked-in fixture and every mapping file used elsewhere in this README still load unaffected |
| Confirmed capability, no new code needed: a source field can already be given *two* dispositions at once — mapped via `fields:` (e.g. `lookup:` + `on_missing: null`) **and** separately preserved raw via `unmapped.jsonb` — `EntityMapping` never enforced disposition-exclusivity. This directly answers a real design question (preserve a dangling reference's original value for forensics, without an FK to a value that isn't there) without adding a dedicated "legacy/raw copy" construct to the mapping DSL | ✅ — live-tested: a dangling `mcmUserId` landed as `user_id = NULL` (per `on_missing: null`) *and* `raw_payload = {"mcmUserId": "<original ObjectId hex>"}` in the same row, `validate` reporting zero mismatches |
| Collection coverage (`validate-mapping --mongo-uri`): every collection actually present in the source database is now checked against the mapping file's entities and a new `excluded_collections: [...]` list — found by re-reading an earlier review verbatim rather than from memory: "a collection simply absent from the mapping file is never mentioned by any command... 'deliberately not migrating this' and 'forgot this existed' are indistinguishable." Non-blocking (a real database can hold plenty of genuinely irrelevant collections), but no longer invisible — a warning names exactly which collection has no disposition | ✅ — live-tested: an added `auditLogs` collection with no entity and no `excluded_collections` entry correctly produced a warning naming it; adding it to `excluded_collections` cleared the warning with no other change; a discriminator-filtered pair sharing one `source:` collection correctly counts as covered once, not flagged twice |
| Unmapped-field policy, one level down: `ExplodeSpec` now carries its own `unmapped: {drop, jsonb, jsonb_column}` — same shape, same real-jsonb-landing guarantee as the top-level `EntityMapping.unmapped` — and `validate-mapping --mongo-uri` checks fields *inside* every exploded array item against it, recursively through nested `explode`. Found the same way as collection coverage: re-reading the *earliest* PRD design review verbatim (written before any code existed) turned up "nested-path unmapped checks inside exploded objects... acceptable to decide in code" — a question that was flagged, never actually decided, for the entire life of the project | ✅ — live-tested: `items[].discount`/`items[].note` with no disposition correctly blocked `validate-mapping`; adding `unmapped: {jsonb: [discount, note], jsonb_column: extra}` cleared it, and `migrate` landed the real values per row (`{"note": "gift wrap", "discount": 0.1}` / `{"note": null, "discount": 0}`) — not just accepted as a label; a misconfigured `jsonb_column` name hard-fails `migrate` before any write, mirroring the top-level check exactly; the base demo fixture (fully mapped, nothing to flag) still passes with zero new warnings |
| Live integration tests in CI (`tests/integration/`, its own CI job with real Mongo/Postgres service containers): the "live-tested" claims scattered through this table were previously proven once, by hand, in a dev session, and never re-checked — another old review, re-read verbatim: "consider capturing them as a compose-based integration test so CI proves them, not prose." A first slice: the full `validate-mapping`→`dry-run`→`migrate`→`validate` loop through the actual CLI (`CliRunner`, real Mongo + real Postgres, PRD §12's own worked example), and a genuine SIGKILL-mid-migrate-then-resume test — a real subprocess, killed via polling for actual partial progress (not a guessed sleep), asserting zero duplicates and zero gaps after resuming. Skipped cleanly (not failed) when `MONGO_URI`/`POSTGRES_URI` aren't set, so the plain unit-test suite stays exactly as fast as before | ✅ — both pass reliably against local Docker (3/3 repeated runs, no flakes) and now run in CI on every push |

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

`pytest` alone (no env vars) runs the unit-test suite only — fast, no network, exactly what CI's matrixed `test` job runs. `tests/integration/` (real Mongo + real Postgres, including a genuine SIGKILL-and-resume) runs separately, in its own CI job, and is skipped cleanly by the command above unless `MONGO_URI`/`POSTGRES_URI` are set:

```bash
docker compose up -d
MONGO_URI=mongodb://localhost:27017/app \
POSTGRES_URI=postgresql://postgres:postgres@localhost:55432/app \
pytest -q tests/integration
```

## License

MIT
