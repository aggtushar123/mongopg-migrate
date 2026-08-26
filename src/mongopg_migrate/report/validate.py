"""Post-migration validation: count diff + hashed-field sample diff.

PRD §6 step 7 / §7 / §9: "row counts matching is necessary but not
sufficient; values must be checked too." NOT YET IMPLEMENTED — depends on
migrate/load.py existing first. Spec to build against:

  1. Count diff: per entity, `SELECT count(*)` on the Mongo collection
     (matching whatever filter/discriminator the mapping applied, for
     polymorphic collections — PRD §6 step 3) vs. the target Postgres
     table (and each `explode`/`junction` child table).
  2. Hashed-field sample diff: for a random sample of already-loaded rows
     (looked up via `_mongopg.id_map`), recompute each mapped field's value
     from the source Mongo document (applying the same transform the
     mapping specifies) and compare a hash of the row to a hash of the
     actual Postgres row. A mismatch means the *values* are wrong even
     though the count matched — this is the check that catches transform
     bugs, truncated types, and silent coercion that a count alone would
     miss (PRD §9 "Zero silent data loss").
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mongopg_migrate.mapping.schema import MappingFile


@dataclass
class CountDiff:
    entity: str
    table: str
    mongo_count: int
    postgres_count: int

    @property
    def matches(self) -> bool:
        return self.mongo_count == self.postgres_count


@dataclass
class SampleDiff:
    entity: str
    source_id: str
    mismatched_fields: list[str]


@dataclass
class ValidationReport:
    count_diffs: list[CountDiff] = field(default_factory=list)
    sample_diffs: list[SampleDiff] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.matches for c in self.count_diffs) and not self.sample_diffs


def validate(mapping: MappingFile, mongo_uri: str, postgres_dsn: str, *, sample_size: int = 200) -> ValidationReport:
    raise NotImplementedError(
        "post-migration validation is not yet implemented — see this module's "
        "docstring for the spec (PRD §6 step 7, §7, §9)"
    )
