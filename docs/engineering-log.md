# Engineering log

Every entry below was written while building `mongopg-migrate`, mostly after
finding a bug the hard way. They are kept because the *reasoning* is worth
having — but they used to sit in the README under a single `✅` and the words
"live-tested", which read as a uniform guarantee that did not exist.

Each entry now states what actually backs it:

| Label | Means |
|---|---|
| **Automated (CI)** | Tests in `tests/`, run on every push. |
| **Automated against real Mongo + Postgres (CI)** | Tests in `tests/integration/`, run against real service containers on every push. |
| **Partly automated** | Part of the claim is tested; the rest was checked by hand. The entry says which is which. |
| **Hand-verified only — not automated** | Checked once, by hand, in a development session. Nothing in this repo re-checks it. |
| **Unverified — no test covers this** | Exactly that. |

A hand-verified claim is not a false one — it is an unreproducible one. Where
an entry quotes a specific figure from such a session, that figure came from
data that is not in this repository and cannot be re-derived from it.

---

## Mongo introspection (sampling, type/variance inference, polymorphism detection)

**Automated (CI)** — `test_mongo_introspect.py` (5)




## Postgres introspection (schema, FKs, FK-derived load order)

**Automated (CI)** — `test_postgres_load_order.py` (4)

Covers the FK-graph and load-order logic. The `information_schema` queries themselves are exercised only by the live tests.




## Mapping-file format + structural/unmapped-field validation

**Automated (CI)** — `test_mapping_schema.py` (17)




## Rule-based candidate mapping proposer

**Automated (CI)** — `test_propose.py` (3)




## Batch loader: entity-ordered COPY, `_mongopg.id_map`, per-batch checkpoint/resume, `truncate`/`append`

**Automated against real Mongo + Postgres (CI)** — `integration/test_kill_resume.py`, `integration/test_live_roundtrip.py`; `test_load_truncate.py` (2)

The SIGKILL-and-resume claim is now genuinely automated. The original '4002 docs' figure was a one-off development run and is not reproducible from this repo.


Observed including a real SIGKILL mid-run + resume (4002 docs, zero dupes/orphans)

## Dry-run: Layer A (in-memory type/null/lookup checks) + Layer B (real COPY+FK load into a disposable schema clone)

**Automated (CI)** — `test_dryrun_fast_pass.py` (4); `integration/test_live_roundtrip.py`


Observed: catches a real lookup miss before any write, leaves zero artifacts on success or failure

## Post-migration validation: count diff (incl. explode/junction/unpivot tables) + hashed-field sample diff

**Automated (CI)** — `test_validate_canonicalize.py` (12); `integration/test_live_roundtrip.py`


Observed: catches a real corrupted value with the exact field + row identified, clean data passes

## `--mode upsert`: staging table + `ON CONFLICT DO UPDATE` for the main entity and `junction` tables; `explode` children always plain-insert (no natural conflict key)

**Automated against real Mongo + Postgres (CI)** — `test_load_upsert.py` (4), `integration/test_upsert_live.py` (6)

The live tests pin two behaviours the SQL alone does not show: a document
MODIFIED in the source is not picked up while the entity's checkpoint stands
(the run reports "already fully loaded" and exits 0), and re-processing a
document duplicates its `explode` children, which `validate` then catches as
a count mismatch.




## `unmapped.jsonb` landing: serialized into one JSON object, written via a real `jsonb` column (`unmapped.jsonb_column`) — was previously a silent no-op indistinguishable from `drop`

**Partly automated** — 3 jsonb-validation tests in `test_review_gap_fixes.py`, plus `test_explode_unmapped.py`

The mapping-level validation is automated. The claim that string/float/list/datetime all round-trip through COPY was verified by hand and is not automated.


Observed: string/float/list/datetime all round-trip correctly through COPY via psycopg's `Jsonb` wrapper

## Discriminator-filtered mappings (`filter: {field, equals}`, PRD §7 P0 — the last previously-unbuilt P0): closes the loop from detection (already built) to an actual multiple-mappings-per-collection capability

**Automated (CI)** — 3 filter tests in `test_review_gap_fixes.py`


Observed: one polymorphic collection split into two entities/tables, correctly-scoped counts and dry-run checks per filter. `validate-mapping`'s unmapped-field check is filter-aware too (`introspect_entities`, keyed by entity + `entity.mongo_filter()`) — observed a field only present on the *other* discriminator value no longer shows as a false "no disposition"

## `unmapped.jsonb` sample-diff value-checking: the jsonb payload is recomputed from the source doc (`migrate.transform.json_safe`, shared with the loader) and compared to what Postgres actually has

**Automated (CI)** — 2 jsonb-hash tests in `test_validate_canonicalize.py`


Observed: a directly-corrupted jsonb value is caught with the column named; a reordered-but-identical jsonb value (Postgres doesn't preserve key insertion order) correctly does *not* false-positive

## `int_sequence` batched id reservation: one `SELECT nextval(seq) FROM generate_series(1, N)` round trip per block instead of one `nextval()` per document

**Partly automated** — 6 `int_sequence` tests in `test_idstrategy.py`

Block reservation, refill and per-sequence keying are automated. The '1200 documents in exactly 3 round trips, verified against Postgres's own query log' measurement was a hand-run session and is not reproducible here.


Observed against Postgres's own query log: 1200 documents served by exactly 3 `generate_series` round trips (500+500+200, matching the block size), zero individual `nextval()` calls; all 1200 ids unique and correct

## Cross-run lookups (`external_entities`, PRD §12's own worked example): `lookup:` can name an entity migrated in an *earlier*, separate run, resolved via `_mongopg.id_map` instead of requiring every referenced entity to be declared in the same mapping file

**Partly automated** — 2 external-entity tests in `test_review_gap_fixes.py`

That a declared external entity is accepted, and that it does not mask a typo, are automated. The two-separate-runs end-to-end was verified by hand.


Observed: two independent `migrate` runs against two separate mapping files, FK correctly resolved across them

## `append`/`upsert` resuming a `done` entity: previously frozen after first completion — new documents inserted since required manually deleting the checkpoint row

**Automated against real Mongo + Postgres (CI)** — `integration/test_resume_done_entity_live.py` (8)

Covers both `append` and `upsert`: documents inserted after completion are
picked up, the checkpoint advances, a run with nothing new is a clean no-op
that writes nothing and does not move the checkpoint, and `validate` agrees
afterwards. Re-freezing the entity — the original bug — fails 6 of the 8.


Observed: new document picked up automatically on next run, "nothing new" case still reports cleanly

## Cross-**database** lookups (`external_databases`, a microservices split — N target Postgres databases from one Mongo source): a `lookup:` entity can live in a genuinely different database, not just the same one from an earlier run. Maps entity name -> an env var holding that database's connection string (never a raw credential in the checked-in mapping file)

**Partly automated** — `test_cross_database_lookups.py` (11)

Connection handling, validation and the internal-schema rule are automated. The migration across two genuinely separate databases was verified by hand.


Observed with two actually-separate Postgres databases (not schemas): migrated `hospitals` into one, then `bookings` (referencing it via `lookup:`) into a completely different one, over a live cross-database connection — FK resolved correctly, `dry-run`/`validate` both cross-database-aware too. One real bug caught and fixed by this same live test: Layer B's disposable internal-schema name was leaking into the external entity's lookup, which must always use its own database's real `_mongopg` schema regardless of what this run calls its own — now covered by both the live test and a fast unit regression test

## `enum:`/`split:` transforms — the last two items in the PRD's P0 transform DSL list, both prompted by a real user hitting exactly these gaps in a Prisma→Postgres migration

**Automated (CI)** — `test_transform.py` (25)


`enum:<json mapping>` remaps a value through an explicit table (`"*"` wildcard for a fallback, otherwise a loud error on an unlisted value); `split:<delimiter>` turns a delimited string into a list for a Postgres ARRAY column. Live-tested, both directions. Also discovered live: a Mongo array field mapped **without any transform at all** already lands correctly on a Postgres ARRAY column (psycopg's COPY path adapts Python lists automatically) — not a gap that needed closing, just needed confirming and documenting

## CLI: `introspect`, `propose`, `validate-mapping`, `migrate`, `dry-run`, `validate`

**Automated against real Mongo + Postgres (CI)** — `integration/` (6, via click's `CliRunner`)




## Docker image (primary distribution, PRD §8)

**Hand-verified only — not automated**

Built and run by hand; `release.yml` builds it multi-arch on a version tag. No test asserts the image behaves correctly.


Observed: builds clean, every command run from inside the container against the fixture over the compose network

## LLM-assisted mapping suggestions (`propose --llm`, PRD §7 P1/§8): pluggable `LLMClient` seam, schema-metadata-only payload, never trusts a suggestion blindly

**Automated (CI)** — `test_llm_propose.py` (15), `test_llm_client_openai_compatible.py` (12)

The `openai-compatible` provider is tested against a real local HTTP server. The Anthropic provider is NOT live-verified — see the note in the entry.


Provider-agnostic via `--llm-provider`: `anthropic` (Anthropic API) or `openai-compatible` (plain HTTP to any server speaking the OpenAI chat-completions contract — OpenAI, Azure OpenAI, Ollama, vLLM, LM Studio, llama.cpp server, ..., zero added dependency). `openai-compatible` is observed against a real local HTTP server (12 tests, genuine socket round-trips). `anthropic` is **not** verified end-to-end (no credentials in this dev environment): unit-tested against a fake client, and one real network call with a deliberately invalid key confirmed a genuine 401 from Anthropic's servers, not a client-side SDK-usage error

## Explode/junction array-shape safety (PRD §7 P0): `doc.get(field) or []` silently iterated a truthy scalar string character-by-character (e.g. a plain `department: "CARDIOLOGY"` mapped via `junction:` produced 10 silent one-letter rows, no error) — found by testing the loader against a real mapping. Loader now hard-fails with the field name/type/value and a fix suggestion (`LoadError`); dry-run Layer A reports it as a violation instead of writing anything

**Automated (CI)** — `test_scalar_array_safety.py` (16)


16 regression tests, including a direct reproduction of the character-by-character iteration

## `cast_bool`/`cast_text` array safety: found live re-verifying a transcript's "arrays of plain scalars" note (already resolved — see the `enum:`/`split:` row above — but re-checking it directly, rather than trusting the earlier note, surfaced this) — `bool(some_list)` is `True` for any non-empty list and `False` for an empty one (Python truthiness, not a real cast), and `str(some_list)` silently lands the Python repr `'[1, 2, 3]'` in a text column; neither raised. `cast_int`/`cast_float` already rejected a list on their own (`int()`/`float()` naturally do); `enum:`/`split:` already raised loudly too — only `cast_bool`/`cast_text` were quietly wrong, same footgun class as the character-by-character scalar-iteration bug above

**Automated (CI)** — `test_transform.py`


5 regression tests; observed: a `flags: ["a", "b"]` field mapped with `transform: cast_bool` now fails dry-run with a clear message instead of silently landing `is_active = true`

## `unpivot:` construct (PRD §7 P0, worked example §12.2): N differently-named top-level scalar fields (e.g. `pfAmount`/`payToHospital`/`finalBill`) → N rows in an existing child table, each carrying a literal `code` — the EAV/pivot-normalization shape neither `explode` (one array, repeated shape) nor `junction` (one array of scalar FKs) can express. Natural key `(parent_fk, code)` makes `--mode upsert` genuinely meaningful (unlike `explode` children)

**Automated (CI)** — `test_unpivot.py` (13)


Observed: mixed presence/absence/explicit-null across 3 documents produced exactly the expected 5 rows (`skip_null` respected), re-running in upsert mode updated one row's value in place with zero duplicates; count-diff (`bookings_test.amounts: mongo=5 postgres=5`) and dry-run Layer A (transform errors + NOT NULL, respecting `skip_null`) both cover it

## Nested `explode:` (PRD §7 P0, worked example §12.1) — a second embedded array one level down (e.g. `hospitalDetails.facilities[].categoryParts[]` → a `HospitalFacility` row per facility, each with its own `FacilityCategoryPart` child rows). A middle level's own id is resolved *before* its row is COPYed (`resolve_new_id`, previously called only for the top-level entity) so it can be threaded down as the nested level's `parent_fk` — `explode.id_strategy` was a validated-but-unread field before this; `serial` is now rejected on any level that has nested children, since a SERIAL value isn't known until after INSERT and COPY has no RETURNING

**Automated (CI)** — `test_nested_explode.py` (15)


Observed: 2 hospitals / 3 facilities / 3 category-parts loaded with correct FK threading at both levels (verified by joining all three tables back together), a facility with no `categoryParts` at all produced zero grandchild rows, a scalar-where-array-expected mistake was caught by dry-run *and* hard-failed migrate with a clean rollback + resume rather than writing anything wrong, count-diff correctly reports both nesting levels (`hospitals_test.facilities.categoryParts: mongo=3 postgres=3`)

## Fan-in reshape helper (PRD §4 non-goal — deliberately **not** part of the mapping DSL): `mongopg-fanin`, a separate installed command, for the "N Mongo documents → 1 target row" case (e.g. latest `KycVerificationStep` per booking) no mapping construct can express. Wraps the standard `$match → $sort → $group → $replaceRoot → $out\

**Partly automated** — `test_fanin_reshape.py` (21)

`mongomock` does not implement `$merge`, so that write path is verified by hand against a real Mongo rather than in CI.


$merge` pattern with dry-run preview, a confirmation gate, and clean errors — then you point `mongopg-migrate` at the resulting already-1:1 derived collection like any other. See [`docs/fanin-reshape.md`](./docs/fanin-reshape.md) | ✅ — observed: 6→3 doc reduction with correct latest-status-wins-per-group and `--pick-order asc` (earliest-wins) both confirmed, `--mode merge` verified to leave an unrelated pre-existing document untouched (vs. `--mode out` replacing the whole collection), the missing-unique-index failure `$merge` requires surfaces a clear actionable error instead of a raw traceback, the derived collection round-tripped through the full `validate-mapping`/`dry-run`/`migrate`/`validate` pipeline with zero special-casing

## `on_missing: error\

**Automated (CI)** — `test_on_missing.py` (37)


Null\|skip_row` (PRD §7 P0, worked example §12.3) — policy for a *dangling* `lookup:` (source value present, but nothing resolves it, e.g. the referenced document was deleted). Previously an unconditional hard-fail with no way to say "I know about this." `null` writes NULL (still fails against a genuinely NOT NULL column — a policy can't rescue a real schema mismatch); `skip_row` drops the row the field's value belongs to — the whole document for a top-level field, one array item for `explode`, one join row for `junction` (`junction` only accepts `error`/`skip_row`, never `null` — `child_fk` is half the row's own identity). Every occurrence counted and reported at migrate, dry-run (as a non-blocking info notice), and independently re-derived at `validate` (count-diff reconciles the known `skip_row` reduction; sample-diff correctly matches a `null`-rescued row instead of false-flagging every one) | ✅ — observed against a real dangling-reference scenario (a `KycVerificationStep` referencing a deleted `McmUser`, plus a `junction` tag reference to a deleted tag): default `error` still hard-fails identically to before; `null` correctly nulled the one dangling row while the other two resolved normally, with `validate` showing zero false-positive mismatches (the exact bug this fix targets) and independently re-deriving the same dangling count; `skip_row` correctly dropped only the affected document (junction: only the affected join row, parent order row intact) while advancing the checkpoint past it (confirmed no infinite-retry on re-run) — a real, live-caught bug fixed in the same pass: `validate`'s count-diff didn't reconcile `skip_row`'s deliberate reduction and reported a clean migration as `Validation FAILED`; also fixed live: bare `on_missing: null` in YAML parses to Python `None`, not the string `"null"` — now coerced rather than rejected with a confusing enum error

## Duplicate-key safety in the mapping file: `yaml.safe_load`'s default silent last-one-wins for a repeated key (two `fields:` entries for the same source field, two entities with the same name, ...) now raises loudly at `load_mapping_file` instead — found live, writing a mapping that mapped one source field twice (once via `lookup:`, once as a raw passthrough copy): the first entry vanished with zero warning. Same footgun class as the scalar-iteration and bare-`null` bugs above

**Automated (CI)** — 3 duplicate-key tests in `test_mapping_schema.py`


A custom `yaml.SafeLoader` subclass overrides `construct_mapping` to detect the collision before pydantic ever sees the (already-collapsed) dict; observed both that the duplicate case now raises with a clear message and that the checked-in fixture and every mapping file used elsewhere in this README still load unaffected

## Confirmed capability, no new code needed: a source field can already be given *two* dispositions at once — mapped via `fields:` (e.g. `lookup:` + `on_missing: null`) **and** separately preserved raw via `unmapped.jsonb` — `EntityMapping` never enforced disposition-exclusivity. This directly answers a real design question (preserve a dangling reference's original value for forensics, without an FK to a value that isn't there) without adding a dedicated "legacy/raw copy" construct to the mapping DSL

**Unverified — no test covers this**

A claim about behaviour that already existed, with no test pinning it. If the exclusivity assumption it relies on were tightened, nothing here would notice.


Observed: a dangling `mcmUserId` landed as `user_id = NULL` (per `on_missing: null`) *and* `raw_payload = {"mcmUserId": "<original ObjectId hex>"}` in the same row, `validate` reporting zero mismatches

## Collection coverage (`validate-mapping --mongo-uri`): every collection actually present in the source database is now checked against the mapping file's entities and a new `excluded_collections: [...]` list — found by re-reading an earlier review verbatim rather than from memory: "a collection simply absent from the mapping file is never mentioned by any command... 'deliberately not migrating this' and 'forgot this existed' are indistinguishable." Non-blocking (a real database can hold plenty of genuinely irrelevant collections), but no longer invisible — a warning names exactly which collection has no disposition

**Automated (CI)** — 4 collection-coverage tests in `test_mapping_schema.py`


Observed: an added `auditLogs` collection with no entity and no `excluded_collections` entry correctly produced a warning naming it; adding it to `excluded_collections` cleared the warning with no other change; a discriminator-filtered pair sharing one `source:` collection correctly counts as covered once, not flagged twice

## Unmapped-field policy, one level down: `ExplodeSpec` now carries its own `unmapped: {drop, jsonb, jsonb_column}` — same shape, same real-jsonb-landing guarantee as the top-level `EntityMapping.unmapped` — and `validate-mapping --mongo-uri` checks fields *inside* every exploded array item against it, recursively through nested `explode`. Found the same way as collection coverage: re-reading the *earliest* PRD design review verbatim (written before any code existed) turned up "nested-path unmapped checks inside exploded objects... acceptable to decide in code" — a question that was flagged, never actually decided, for the entire life of the project

**Automated (CI)** — `test_explode_unmapped.py` (12)


Observed: `items[].discount`/`items[].note` with no disposition correctly blocked `validate-mapping`; adding `unmapped: {jsonb: [discount, note], jsonb_column: extra}` cleared it, and `migrate` landed the real values per row (`{"note": "gift wrap", "discount": 0.1}` / `{"note": null, "discount": 0}`) — not just accepted as a label; a misconfigured `jsonb_column` name hard-fails `migrate` before any write, mirroring the top-level check exactly; the base demo fixture (fully mapped, nothing to flag) still passes with zero new warnings

## Nested `lookup:` invisible to load ordering — a real bug an external reviewer found and reproduced against this exact code, then reproduced again after a fix attempt to confirm it: `entity_dependencies()`/`entity_load_order()` and `validate_structure()` only ever iterated one explode level's own `.fields`, so a `lookup:` one level deeper (`facilities[].categoryParts[].lookup: zcategories`) was invisible to both — wrong load order (unenforced by `entity_load_order()`), and a typo'd nested `lookup:` name passed `validate_structure()` with zero issues. **`on_missing` made the load-order half of this silent, not just wrong**: before `on_missing` existed, an empty id_map from the wrong order was a loud `LoadError`; with `on_missing: null`, every reference then quietly writes NULL, count diff is unaffected (NULLs don't change row counts), `validate`'s own `_count_on_missing` re-derives against the by-then-fully-loaded id_map and reports zero dangling refs, and `validate` reports OK — an all-NULL FK column that passes every check. Fixed at both the cause and the blast radius: `entity_dependencies()`/`validate_structure()` now recurse through nested `explode` (root cause); independently, `_resolve_lookup` now refuses to apply *any* `on_missing` policy when the referenced entity's id_map has zero rows at all — cached per entity, one extra indexed query on the first miss, not per miss — since a policy for one dangling reference is not a correct answer to "this entity never loaded," whether from the ordering bug just fixed or a forgotten prerequisite run (`external_entities` naming a migration nobody actually ran) that no amount of correct-ordering logic *can* catch

**Automated (CI)** — `test_entity_load_order.py`, `test_nested_explode.py`, `test_on_missing.py` (3 never-loaded cases)


Observed all three shapes: the exact reported reproduction (`entity_dependencies()`/`entity_load_order()` now correctly order `zcategories` before `hospitals`, migrated end to end with the nested FK correctly resolved, not NULL); a simulated forgotten-prerequisite-run (`external_entities` naming a migration that was never run, `on_missing: null`) now hard-fails with a message distinguishing "load-order problem" from a real dangling reference, instead of silently writing NULL; a genuinely dangling individual reference (the entity has other rows, just not this one) still correctly nulls as designed — confirming the fix narrows precisely, not just broadly

## Live integration tests in CI (`tests/integration/`, its own CI job with real Mongo/Postgres service containers): the "live-tested" claims scattered through this table were previously proven once, by hand, in a dev session, and never re-checked — another old review, re-read verbatim: "consider capturing them as a compose-based integration test so CI proves them, not prose." A first slice: the full `validate-mapping`→`dry-run`→`migrate`→`validate` loop through the actual CLI (`CliRunner`, real Mongo + real Postgres, PRD §12's own worked example), and a genuine SIGKILL-mid-migrate-then-resume test — a real subprocess, killed via polling for actual partial progress (not a guessed sleep), asserting zero duplicates and zero gaps after resuming. Skipped cleanly (not failed) when `MONGO_URI`/`POSTGRES_URI` aren't set, so the plain unit-test suite stays exactly as fast as before

**Automated against real Mongo + Postgres (CI)** — `integration/` (6)


Both pass reliably against local Docker (3/3 repeated runs, no flakes) and now run in CI on every push

## `--pg-schema` honoured on the WRITE path, not just introspection: it now sets `search_path` explicitly for `migrate`/`dry-run`/`validate`, and a schema that does not exist is reported rather than read as an empty target

**Automated (CI)** — `test_target_schema.py` (11), `integration/test_target_schema_live.py` (4)

All four live tests were confirmed to fail against the pre-fix code.


CI-proven: `tests/test_target_schema.py` (11) + `tests/integration/test_target_schema_live.py` (4, real Postgres). The live tests put an identically-named decoy table in `public`, and all four were confirmed to fail against the pre-fix code

## Transform pipelines (`a\

**Automated (CI)** — `test_transform_pipeline.py` (37)


B`, left to right) plus the `truncate:<n>` and `trim` transforms — one field can need two unrelated fixes and `transform:` has one slot | ✅ — CI-proven: `tests/test_transform_pipeline.py` (37), including the `default:`-is-not-re-piped edge and a `\|` inside an `enum:` JSON object

## `depends_on:` — a declared load-order edge the `lookup:` graph cannot infer (an FK resolved through hand-seeded `external_entities` rows names no `lookup:`, so it floated to the front and COPYed before its parent)

**Automated (CI)** — `test_depends_on.py` (14)


CI-proven: `tests/test_depends_on.py` (14), covering ordering, union with lookup-derived edges, cycles, and the typo/self-reference/external-name validation

## id_map bulk `prefetch()` + batched `put_many()`: `get()` was one network round trip per `lookup:` per row, which over a VPN set the pace for the whole migration

**Automated (CI)** — `test_idmap_prefetch.py` (21)

The two subtlest behaviours were mutation-checked.


CI-proven: `tests/test_idmap_prefetch.py` (21), covering write-through, per-connection/schema/entity keying, weakref cleanup, chunking under Postgres's placeholder cap, and that a snapshot miss does NOT fall through to a query
