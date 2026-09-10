# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

While the major version is `0`, the mapping-file format and the CLI surface
may still change between minor versions. Breaking changes are called out
under **Changed** with a migration note.

## [Unreleased]

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
  from a build machine.

### Changed

- `load()` and `validate()` take `target_schema`; `search_path` is now a
  sequence of schema names composed through psycopg's identifier quoting
  rather than a raw SQL string. This is an internal API change.
- Removed `migration/` and `table-definitions/`, which were specific to one
  deployment rather than part of a general-purpose tool.

[Unreleased]: https://github.com/aggtushar123/mongopg-migrate/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/aggtushar123/mongopg-migrate/releases/tag/v0.1.1
