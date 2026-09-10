# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

While the major version is `0`, the mapping-file format and the CLI surface
may still change between minor versions. Breaking changes are called out
under **Changed** with a migration note.

## [Unreleased]

### Added

- `--internal-schema` and `--uuid-namespace`. Both were parameterised in code
  and unreachable from outside it. The first renames the schema holding
  `id_map`/`load_checkpoint`, for a target whose owner will not grant
  `_mongopg`. The second overrides the uuid5 namespace behind
  `objectid_to_uuid`, so an organisation that minted ObjectId-derived UUIDs in
  an earlier cutover can reproduce them — previously impossible without editing
  installed source. Changing the namespace partway through a migration is
  refused, because a resume under a different one would insert a second copy of
  every row rather than continue.
- Tests for `validate`'s count-diff correctness
  (`tests/test_validate_countdiff.py`, 28) — the four fixes that stop a correct
  migration being reported as failed, and the newest logic in the safety net,
  had no direct tests at all.

### Fixed

- A failed database connection printed a raw stack trace — a pymongo topology
  dump or psycopg's duplicated IPv6/IPv4 refusal — rather than a message. It is
  the most likely first error after `pip install`, and the only path in the CLI
  that was not reported properly. It now names which database, echoes the URI
  used (with any password masked), gives the reason without the dump, and
  suggests what to check.

## [0.1.1] — 2026-09-10

First published release. `0.1.0` existed in `pyproject.toml` but was never
tagged or distributed, so everything below is new to anyone installing this.

### Fixed

- **`--pg-schema` was ignored on every write path.** It reached
  `introspect_postgres()` and nothing else. Because every write names its
  table without a schema qualifier, the destination was decided by the
  connecting role's `search_path` — normally `public`. A target schema other
  than `public` was therefore read correctly and written elsewhere, with no
  error: the load succeeded against whatever tables it found, and `validate`
  then counted those same wrong tables and agreed with itself.

  `migrate`, `dry-run` and `validate` now set `search_path` explicitly from
  `--pg-schema`. **If you ran an earlier build against a named schema, check
  where the rows actually landed.**
- A `--pg-schema` naming a schema that does not exist returned an empty
  introspection result, so a typo surfaced later as a confusing mapping error
  against a target with no tables. It is now reported directly, listing the
  schemas that do exist.
- The loader ignored `id_strategy.source_field` and always hashed `_id`,
  while `dry-run` already honoured it — so a mapping could pass `dry-run` and
  then fail the real load with a foreign-key violation.
- A `|` inside an `enum:` JSON object was treated as a pipeline separator, so
  `enum:{"A|B": "both"}` was chopped mid-JSON and failed with an
  unterminated-string error naming neither the pipeline nor the pipe — even
  when `enum:` was the only transform on the field. Splitting is now aware of
  JSON nesting and strings. `split:` on a literal pipe combined with other
  steps stays ambiguous by construction, and now says so.
- An embedded object mapped onto a `json`/`jsonb` column failed `COPY`
  (`cannot adapt type 'dict'`). jsonb was previously reachable only through
  `unmapped.jsonb`.
- `cast_timestamptz` raised a bare `ValueError` on an unparseable string
  (including `""`), killing the run with a traceback instead of being
  collected as a per-field violation. `dry-run` in particular is meant to
  gather these, not stop at the first.
- Four `validate` count-diff errors that reported a correct migration as
  failed: two entities sharing one target table were each compared against
  both entities' rows; `skip_row` was counted per distinct value rather than
  per row; children of a skipped parent were not subtracted; and
  `numeric(p,s)` rounding was reported as value corruption.
- `mongopg-fanin` falls back to a client-side rebuild when the server refuses
  `$out` into an internal database.

### Added

- **`mongopg-fanin`**, the fan-in reshape helper, is now an installed command.
  It previously lived at `scripts/fanin_reshape.py` and shipped in neither the
  wheel nor the Docker image, so the step the README told you to run first was
  the one thing `pip install` did not provide. It remains deliberately
  separate from the mapping DSL (PRD §4 non-goal) and never touches Postgres.
- `depends_on:` on an entity — declares a load-order edge the `lookup:` graph
  cannot infer, such as a lookup into a hand-seeded external entity.
- `truncate:<n>` and `trim` transforms, and `|` transform **pipelines**
  (`default:X|truncate:32`), which run left to right. Narrowing stays a
  declared, reviewable decision rather than something applied silently.
- Bulk id_map prefetch and batched writes. `idmap.get()` was one network round
  trip per lookup per row; over a VPN that was the dominant cost of every
  entity after the parent table, not a micro-optimisation.
- Packaging metadata for distribution: project URLs, classifiers, keywords,
  and an explicit sdist allowlist so a release cannot pick up untracked files
  from a build machine. A tagged release publishes to PyPI and GHCR.
- Tests for every feature above, which had shipped without any: 72 for
  pipelines/`truncate:`/`trim`/`depends_on`/the id_map fast path, plus live
  tests for the two production paths that had none — `--mode upsert` against a
  real Postgres and resuming an entity already marked `done`. The integration
  suite goes from 6 tests to 20.
- README sections for all of the above: the Transform DSL, load ordering, and
  "Re-running a migration", which documents two behaviours the new live tests
  surfaced — a MODIFIED source document is not picked up while the entity's
  checkpoint stands, and re-processing a document duplicates its `explode`
  children (which `validate` catches, but the loader does not).

### Changed

- `load()` and `validate()` take `target_schema`; `search_path` is now a
  sequence of schema names composed through psycopg's identifier quoting
  rather than a raw SQL string. This is an internal API change.
- Removed `migration/` and `table-definitions/`, which were specific to one
  deployment rather than part of a general-purpose tool.
- **The README status table no longer claims more than the repo can show.** It
  carried ~45 rows marked "✅ — live-tested", which read as a uniform
  guarantee; most were unreproducible notes from a development session, some
  quoting figures derived from data that is not in this repository. The README
  now cites, per capability, the tests that back it, and `tests/test_doc_claims.py`
  fails the build if a citation is wrong. The full history moves to
  `docs/engineering-log.md`, where every entry carries an explicit evidence
  label. One capability is still marked unproven: the Docker image is
  hand-verified, with no test asserting it behaves.

[Unreleased]: https://github.com/aggtushar123/mongopg-migrate/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/aggtushar123/mongopg-migrate/releases/tag/v0.1.1
