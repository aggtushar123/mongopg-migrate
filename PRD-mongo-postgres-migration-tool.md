# PRD: Mongo→Postgres Migration Assistant

## 1. Summary
An open-source CLI/tool that takes two connection strings — a source MongoDB instance and a target PostgreSQL instance whose schema **already exists, independently designed** (not generated from the Mongo shape) — infers a field- and table-level mapping between them (ID strategy, one-to-many splits, load order), has the user confirm that mapping, and executes a validated, repeatable data migration, optionally using LLM-assisted suggestions for renamed, split, or nested fields — without requiring the user to hand-write transform scripts.

## 2. Problem Statement
Teams migrating from MongoDB to a normalized PostgreSQL schema face a specific, well-documented gap: mapping Mongo documents onto a Postgres schema that **already exists and was designed independently of the source** — not derived from it. This is not an empty field. Purrito, ToroDB, AWS DMS, and dlt all generate or derive the target schema *from* the Mongo shape (1:1 flatten or document-store passthrough); the target follows the source. DBDock, the closest OSS analog, goes further — proposing a reviewable mapping file with dry-run before write — but still generates the target schema rather than mapping onto one the team already built. Commercial sync tools (e.g. Stacksync) can map onto existing tables, but as hosted SaaS, not a self-hosted, auditable batch tool. No OSS tool takes "here is my Mongo data, here is the Postgres schema I already designed — figure out and confirm the mapping between them" as its starting point. That step is currently always manual, custom-scripted work: slow, error-prone, and repeated from scratch by every team that does it.

## 3. Goals
- Let a developer point the tool at two connection strings (source Mongo, an **already-existing, independently designed** target Postgres schema) and get a working, reviewed migration with minimal manual scripting.
- Make the mapping step (Mongo fields → Postgres columns/tables, including one-to-many splits, renames, and ID remapping) semi-automatic: proposed by the tool, confirmed by the human.
- Handle the mechanics that make mapping onto a pre-existing schema hard — ID strategy, FK-derived load order, one-collection-to-many-tables — as core functionality, not edge cases deferred past v1.
- Support one-time batch migration first; design for future incremental/CDC sync.
- Ship as OSS so it's auditable and self-hostable — no data leaves the user's environment except optional LLM calls for mapping suggestions (which should be opt-in and configurable/offline-capable).

## 4. Non-Goals (v1)
- **Does not design or `CREATE` the target Postgres schema.** The user brings existing DDL (greenfield design, rewrite, or ORM models already migrated). This is the wedge, not a gap to fill later — a tool that generates the target schema is a different product (see DBDock).
- Not a real-time CDC/replication product (that's a v2 stretch goal).
- Not a GUI-first product — v1 is CLI + a local read-only review UI (e.g. a served HTML page), not a hosted SaaS.
- Not a guarantee of 100% automated correctness — human review of the proposed mapping is a required step, not optional.
- No support for non-Postgres relational targets in v1.
- No application/query-layer rewrite (ORM models, API code) — out of scope; this tool moves data only.
- No GridFS support in v1.
- No automatic resolution of circular foreign keys beyond standard Postgres `DEFERRABLE` constraints — a true cycle the schema doesn't already mark deferrable is a flagged error, not something the tool reorders around.

## 5. Target Users
- Individual developers / small teams doing a one-time Mongo→Postgres migration.
- Teams that have **already designed** the target Postgres schema — via greenfield DDL, a rewrite, or ORM models generated separately — and now need the existing Mongo data moved into it. (Not a fit for teams who want the tool to figure out *what* the normalized schema should look like — that's a non-goal, see §4.)

## 6. User Flow
1. **Connect**: user provides Mongo URI and Postgres URI (via CLI flags or `.env`; read-only credentials recommended for Mongo, write access needed for Postgres).
2. **Introspect**: tool samples each Mongo collection (configurable sample size) and infers field names, types, nesting, and type variance across documents. Tool reads Postgres `information_schema` for target tables, columns, types, and foreign keys.
3. **Propose mapping**: tool generates a candidate mapping — Mongo collection/field → Postgres table/column — including detection of likely one-to-many splits (embedded arrays/nested objects → child tables, possibly one collection exploding into several tables), an inferred `id_strategy` per entity (ObjectId passthrough, UUID generation, serial with lookup table), and detection of polymorphic document shapes within a single collection (e.g. a `type` discriminator field implying multiple target mappings). Ambiguous or low-confidence mappings are flagged, never silently guessed.
4. **Review**: user reviews/edits the mapping via a local review UI or an editable YAML/JSON mapping file. Every Mongo field must resolve to a mapped column, an explicit `drop`, or an explicit `jsonb` fallback — an unmapped field with no disposition fails validation of the mapping file itself, before any connection to Postgres for writing. Nothing is written to Postgres until the mapping is confirmed.
5. **Dry run**: two layers, not one transaction (a full-dataset dry run can't live inside a single ROLLBACK-able transaction). (A) **Fast pass** — validate types/nulls/transform correctness against the sampled or full dataset in memory/staging, no Postgres write at all. (B) **Realistic pass** (optional, recommended before a real migration) — clone every mapped table *and* the `_mongopg.id_map` lookup rows they depend on into a disposable temp schema (`migrate_dryrun_<ts>`), load there via the real COPY + FK path (FKs must resolve within the cloned schema — never a cross-schema FK from temp back to `public`), to catch constraint violations, FK-order issues, and load failures that only show up against live Postgres, then drop the temp schema. Report combines both.
6. **Migrate**: user specifies write disposition: `--mode truncate|append` (P0 — see §7 for `upsert`, which is P1). `truncate` is the default *only* when target tables are confirmed empty at run time; if they already contain rows, the tool refuses to run without an explicit `--mode` flag — no silent destructive default. Tool derives load order from target foreign keys (parents before children, or deferred-constraint loading), executes the batch load using COPY (not row-by-row INSERT) for performance, remaps IDs per the confirmed `id_strategy` (writing each mapping into the tool-owned `_mongopg.id_map` table, in the same checkpoint as the table load it belongs to — see §7/§8), and checkpoints **per table** (including `id_map` state) so a kill mid-run resumes without re-loading a completed parent table or corrupting a partially-written lookup.
7. **Validate**: post-migration report comparing source document counts vs. target row counts per mapped entity, plus hashed-field spot-check diffs on a sample — row counts matching is necessary but not sufficient; values must be checked too.

## 7. Key Features (v1 scope)
| Feature | Priority |
|---|---|
| Mongo schema/type inference from sampled documents | P0 |
| Postgres target schema introspection (incl. FKs) | P0 |
| Candidate field/table mapping generation | P0 |
| Explicit `id_strategy` per entity (ObjectId passthrough / UUID / serial + lookup) with ID remapping applied at load | P0 |
| FK-derived load order (parents before children, or explicit deferred-constraint loading) | P0 |
| Explode/unnest mapping — one collection → N tables, not just field=column | P0 |
| Junction-table **mapping** (not generation — the join table must already exist in the target DDL, per §4's non-goal) for scalar-ID arrays (e.g. `tagIds: [ObjectId]` → existing `post_tags` table), FKs remapped via the same `id_strategy` lookup as regular explode | P0 |
| Small transform DSL in the mapping file: cast, default, split, `json_extract`, enum mapping | P0 |
| Polymorphic document detection (shape variance within one collection) + support for multiple mappings filtered by discriminator | P0 |
| Human-editable mapping file (YAML/JSON) with a worked example checked into the repo (see §12) | P0 |
| Unmapped-field policy enforced at mapping-validation time: every source field must resolve to a column, an explicit `drop`, or an explicit `jsonb` fallback — no silent drop | P0 |
| Tool-owned ID lookup storage: `_mongopg.id_map(entity, source_id, target_id)` in the target Postgres database, written in the same transaction/checkpoint as the table load it belongs to. This is the durable source of truth for cross-entity `lookup:` resolution and for resume; a file export of the same data is optional and secondary. | P0 |
| Explicit write-mode flag: `--mode truncate\|append` | P0 |
| Dry-run / validation mode — two layers: in-memory/staging type-null-transform validation, plus an optional temp-schema COPY+FK pass against a full clone (including `id_map` rows) of the mapped tables in live Postgres | P0 |
| Batch load via COPY with per-table checkpoint/resume (including `id_map` state) | P0 |
| Post-migration report: count diff **and** hashed-field sample diff (not counts alone) | P0 |
| Docker image (primary distribution) + docker-compose test setup | P0 |
| LLM-assisted mapping suggestions for ambiguous fields, renames, and splits | P1 |
| Local review UI (read-only web view of proposed mapping) | P1 |
| Auto-suggest `jsonb` as the proposed disposition for unmapped nested data (still requires human confirmation — see P0 unmapped-field policy above; this is about proposing well, not defaulting silently) | P1 |
| `--mode upsert`: COPY into a staging table, then `INSERT ... ON CONFLICT (pk) DO UPDATE`. (`append` alone stays P0 and only inserts new PKs; a PK conflict under `append` is an error, not a silent upsert.) One-time migrations rarely need this — P1 until real demand shows up. | P1 |
| Incremental/CDC sync mode | P2 (future) |
| Support for additional NoSQL sources (e.g. DynamoDB) | P2 (future) |

## 8. Architecture (proposed)
- **Language**: Python (mature drivers: `pymongo`, `psycopg2`/`asyncpg`; easy for contributors).
- **Core modules**:
  - `introspect/mongo.py` — sampling + type/variance inference; flags shape variance within a collection as a polymorphism candidate
  - `introspect/postgres.py` — schema + FK introspection; produces the dependency graph used for load ordering
  - `mapping/propose.py` — rule-based mapping generation (field/table matching, one-to-many split detection, `id_strategy` inference, discriminator detection for polymorphic collections). The optional LLM-assisted pass lives separately, in `mapping/llm_propose.py` (payload building, suggestion merge — never trusts a suggested column that doesn't exist or is already claimed) and `mapping/llm_client.py` (the pluggable `LLMClient` seam + the default Anthropic-backed implementation) — kept out of propose.py so the rule-based path has no dependency on it, and runs strictly after it, only on fields propose.py already gave up on
  - `mapping/schema.py` — mapping file format (YAML) load/validate; format must express explode/unnest (one collection → N tables), per-entity `id_strategy`, transforms (`cast`, `default`, `split`, `json_extract`, enum mapping), and discriminator-filtered sub-mappings — not just `field: column`
  - `migrate/dryrun.py` — two layers: (1) in-memory/staging validation of types, nulls, and transforms against sampled or full data, no Postgres write; (2) optional load into a disposable temp schema, into which every mapped table *and* the `id_map` rows it depends on are cloned first — FKs must resolve within that temp schema, never cross back to `public` — via the real COPY+FK path, to surface constraint/FK-order failures that only appear against live Postgres, then drop the temp schema
  - `migrate/load.py` — reads `--mode truncate|append` (P0; refuses `truncate` on a non-empty target without an explicit flag — `upsert` is P1, staging-table + `ON CONFLICT DO UPDATE`), derives load order from the Postgres FK graph, applies ID remapping by writing to the tool-owned `_mongopg.id_map(entity, source_id, target_id)` table in the target database (same transaction/checkpoint as the table load), COPY-based batch loading with **per-table checkpointing that includes `id_map` state**, so resume never re-loads a completed parent or leaves a lookup half-written
  - `report/validate.py` — post-migration count/diff report **plus** hashed-field sample diff to catch value-level mismatches that counts alone would miss
- **LLM integration**: pluggable, off by default; when enabled, only schema metadata (field names/types/sample shapes) is sent — never actual row data — to minimize privacy exposure. Configurable to use local models to keep it fully offline-capable.
- **Distribution**:
  - **Primary: Docker image** — the recommended way for most users to run the tool. Avoids Python version/driver setup, works identically across OSes, and gives a clean, disposable execution boundary for a task that writes to production Postgres. Connection strings and mapping file passed via env vars / volume mount, e.g. (verified working against the fixture in `docker-compose.yml`, `docker/Dockerfile`):
    ```
    docker run --rm --network <mongo-and-postgres-network> \
      -e MONGO_URI=mongodb://mongo:27017/app \
      -e POSTGRES_URI=postgresql://postgres:postgres@postgres:5432/app \
      -v $(pwd)/mapping.yaml:/app/mapping.yaml:ro \
      mongopg-migrate:latest migrate /app/mapping.yaml --mode truncate
    ```
  - **Secondary: pip package** (`pip install mongopg-migrate`) — for users who want it as an importable library or already have a Python environment set up.
  - **npm package**: deferred. Would require either a Node/TS rewrite of the core or a wrapper around the Python tool, neither of which is worth the maintenance cost without clear demand from JS-only teams. Revisit only if requested post-launch.
  - GitHub repo includes example projects, sample `mapping.yaml`, and a `docker-compose.yml` for spinning up local Mongo/Postgres test instances to try the tool end-to-end before pointing it at real data.

## 9. Success Metrics
**Release bar (crisp demo test):** given a 5–10 collection Mongo app whose Postgres schema *already exists* (built independently, not generated from the Mongo shape), the tool must: auto-map the majority of fields correctly; clearly flag the rest rather than silently guess; catch type/FK/null issues in dry-run before any write; complete a full load that resumes cleanly after a kill mid-run; produce zero silent data loss. If reaching a working migration still requires a hand-written Python transform for IDs or nested arrays, the core gap this product exists to close is not actually closed — ship blockers, not v1.1 items.

- Time from "two connection strings" to "reviewed mapping ready to run" for a typical 5–10 collection schema.
- % of fields correctly auto-mapped without manual edit, measured against the checked-in fixture app (see §12) — not a vibe estimate. Target: majority auto-mapped, all ambiguous ones clearly flagged rather than silently guessed.
- % of one-to-many splits, ID remappings, and polymorphic-collection cases handled without a hand-written script.
- Zero silent data loss — dry-run + validation report must catch type/constraint/FK-order mismatches before commit, and post-migration diffs must catch value-level mismatches, not just count mismatches.
- Community adoption: GitHub stars/issues/PRs as a proxy for real-world use (natural byproduct, not a primary design target).

## 10. Risks & Open Questions
- **Normalization ambiguity**: some one-to-many splits are inherently guessable only with domain knowledge (e.g., is an embedded array a child table or a JSONB blob?) — mitigated by always requiring human confirmation, never auto-committing silently.
- **Type mismatches**: Mongo's loose typing (e.g., same field as string in some docs, number in others) needs explicit handling/reporting rather than silent coercion.
- **Sampling bias**: a small sample can miss rare type variance and rare polymorphic shapes on large collections, producing a mapping that looks confident but breaks on the full load. Sample size needs to scale with collection size (or scan fully below a size threshold), and dry-run against the full dataset — not just the sample — is what actually catches this before commit.
- **Large collections**: sampling strategy must balance inference accuracy vs. introspection cost on very large collections.
- **ID remapping correctness**: if `id_strategy` changes the primary key (e.g. ObjectId → serial), every referencing collection/field must be remapped consistently via the same lookup, including references embedded inside documents that don't correspond 1:1 to a Postgres FK — a bug here causes silent orphaned or misattributed rows, not a visible failure.
- **LLM privacy**: needs a clear default (off, or metadata-only) so the tool is safe to use on production data by default.
- **Circular foreign keys**: a true FK cycle in the target schema breaks strict parent-before-child load ordering. v1 requires the schema to mark such cycles `DEFERRABLE` (loaded within a deferred-constraint transaction); an un-deferrable cycle is a flagged error at dry-run, not something the tool silently reorders around.
- **Existing-data / truncate footguns**: a one-time migration tool defaulting to destructive writes against a target that turns out to be non-empty (e.g. re-run after a partial failure, or pointed at the wrong database) is a real incident risk — mitigated by the `--mode` requirement in §6/§7 (no silent truncate on non-empty tables).
- **Scope creep**: real-time CDC is tempting to add early but should stay v2 — v1 should nail one-time batch migration well first.

## 11. Rough Roadmap
- **Phase 1 (MVP)**: introspection (incl. polymorphism/variance detection) + rule-based mapping (incl. `id_strategy`, explode/unnest, and junction-table mapping) + mapping file with transform DSL and unmapped-field enforcement + two-layer dry run (in-memory pass, plus optional cloned-temp-schema COPY+FK pass) + `truncate`/`append` write modes + FK-ordered COPY load with `_mongopg.id_map`-based ID remapping and per-table checkpoint/resume + validation report (counts + hashed-field diffs). `upsert` and the LLM path stay out of Phase 1 (P1, Phase 2).
- **Phase 2**: LLM-assisted mapping suggestions + local review UI.
- **Phase 3**: incremental/CDC sync, additional source connectors.

## 12. Example Mapping File (normative sketch)

Worked example: a Mongo `orders` collection with embedded line items and a scalar-ID tag array, mapping onto a Postgres schema that already has `orders`, `order_items`, `order_tags` (an existing join table — this is a *mapping*, not table generation, per §4) and a `users` table (assume `users` was migrated in an earlier run, leaving rows in `_mongopg.id_map`). This section is the mapping-file **format contract**, illustrated on one collection — it is not itself the 5–10 collection checked-in fixture app that §9's release bar requires; that fixture is a separate deliverable this format contract feeds.

Source Mongo document shape:
```json
{
  "_id": ObjectId("64f1a2..."),
  "userId": ObjectId("64f0b1..."),
  "status": "shipped",
  "createdAt": ISODate("2026-01-04T10:00:00Z"),
  "items": [
    { "productId": ObjectId("64e9c3..."), "qty": 2, "price": 19.99 }
  ],
  "tagIds": [ObjectId("64e0aa..."), ObjectId("64e0ab...")]
}
```

Target Postgres DDL (already exists, independently designed):
```sql
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
```

Proposed/confirmed `mapping.yaml`:
```yaml
entities:
  orders:
    source: orders                # Mongo collection
    target: orders                # Postgres table
    id_strategy:
      type: objectid_to_uuid      # deterministic UUIDv5(_id) — stable across resumed/re-run loads
      source_field: _id
      target_field: id
    fields:
      status: status
      createdAt:
        target: created_at
        transform: cast_timestamptz
      userId:
        target: user_id
        lookup: users              # resolve via _mongopg.id_map WHERE entity='users' AND source_id=userId
                                    # (fails dry-run loudly if userId has no match — never nulls it silently)

    # one collection -> N tables: embedded array explodes into a child table
    explode:
      items:
        target: order_items
        id_strategy:
          type: serial             # target table has its own serial PK; no source id to preserve
        parent_fk:
          target_field: order_id
          references: orders.id    # resolved to the *new* UUID generated above, not the Mongo _id
        fields:
          productId:
            target: product_id
            lookup: products
          qty: qty
          price: price

    # scalar-ID array -> junction table (distinct from embedded-object explode above)
    junction:
      tagIds:
        target: order_tags
        parent_fk: { target_field: order_id, references: orders.id }
        child_fk:  { target_field: tag_id,   references: tags.id, lookup: tags }

    unmapped_fields: []            # must be empty, or every remaining source field needs an
                                    # explicit `drop: [...]` / `jsonb: [...]` list — see §6 step 4
```

Notes this example is meant to pin down for implementers:
- `id_strategy` is per-entity and explicit, including for child tables produced by `explode` (which may need their own strategy, e.g. `serial`, distinct from the parent's).
- `lookup: <entity>` resolves against `_mongopg.id_map` — a table the tool owns in the target Postgres database, keyed `(entity, source_id, target_id)`, written in the same checkpoint as the load it results from. This is the one and only place ID remappings live; a file export is optional and never authoritative. This is the mechanism referenced generically as "ID remapping via lookup" elsewhere in this PRD.
- `explode` (embedded object/array → child table) and `junction` (scalar-ID array → existing many-to-many join table) are distinct constructs in the format — conflating them was a gap in the earlier draft. Neither construct creates a table; both map onto one that already exists in the target DDL.
- `unmapped_fields` must resolve to empty or an explicit disposition; this is what the P0 unmapped-field policy in §7 actually validates against.
- `upsert` is intentionally absent from this example — it's P1 (see §7); this sketch only needs to support `truncate`/`append`.
