"""Mapping-file format: load, validate, and structurally check a mapping.yaml
against introspected Mongo/Postgres schemas.

This is a direct implementation of the format sketched in PRD §12
(`Example Mapping File (normative sketch)`). Two constructs matter most and
are kept structurally distinct, per the PRD's own note that conflating them
was a gap in an earlier draft:

- `explode`: an embedded object/array field becomes rows in an existing
  child table (one collection -> N tables).
- `junction`: a scalar-ID array field becomes rows in an existing many-to-
  many join table.

Neither construct creates a table — both map onto one that already exists
in the target DDL (PRD §4 non-goal: this tool does not `CREATE` schema).

The other load-bearing rule enforced here is the P0 "unmapped-field policy"
(PRD §7): every field observed on the Mongo side during introspection must
resolve to a mapped column, an `explode`, a `junction`, or an explicit
`unmapped.drop` / `unmapped.jsonb` entry. A field with no disposition is a
validation error, not a silent drop.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class IdStrategyType(str, Enum):
    """How an entity's identity is established in the target table.

    - OBJECTID_TO_UUID: deterministic UUIDv5(source_field) — stable across
      resumed/re-run loads (PRD §12 example).
    - INT_SEQUENCE: source id preserved via a generated integer, recorded in
      `_mongopg.id_map` so other entities can resolve it (PRD §7 id_map row).
    - PASSTHROUGH: source id written as-is (e.g. ObjectId hex into a text/
      bytea column) — still recorded in id_map for uniform lookup semantics.
    - UUID_GENERATE: a fresh random UUID with no deterministic tie back to
      the source id. Non-resumable-deterministically; prefer OBJECTID_TO_UUID
      when the source field is stable.
    - SERIAL: no source id to preserve at all — used for child tables
      produced by `explode` that get their own auto-generated PK and are
      never the target of a cross-entity `lookup:`.
    """

    OBJECTID_TO_UUID = "objectid_to_uuid"
    INT_SEQUENCE = "int_sequence"
    PASSTHROUGH = "passthrough"
    UUID_GENERATE = "uuid_generate"
    SERIAL = "serial"


class IdStrategy(BaseModel):
    type: IdStrategyType
    source_field: str | None = None
    target_field: str = "id"

    @model_validator(mode="after")
    def _source_field_required_unless_serial(self) -> IdStrategy:
        if self.type != IdStrategyType.SERIAL and not self.source_field:
            raise ValueError(
                f"id_strategy.source_field is required for type={self.type.value!r} "
                "(only 'serial' — no source id to preserve — may omit it)"
            )
        return self


class FieldSpec(BaseModel):
    """A single field=column mapping. Accepts the shorthand string form
    (`status: status`) or the full form (`target`/`transform`/`lookup`)."""

    target: str
    transform: str | None = None
    # Name of another entity in this mapping file whose id_strategy lookup
    # (i.e. a row in _mongopg.id_map) resolves this field's value. PRD §12.
    lookup: str | None = None

    @classmethod
    def coerce(cls, value: Any) -> FieldSpec:
        if isinstance(value, str):
            return cls(target=value)
        if isinstance(value, FieldSpec):
            return value
        return cls(**value)


def _normalize_field_map(v: Any) -> dict[str, Any]:
    if v is None:
        return {}
    return {k: FieldSpec.coerce(val) for k, val in v.items()}


class ForeignKeyRef(BaseModel):
    target_field: str
    references: str  # "table.column"
    lookup: str | None = None

    @field_validator("references")
    @classmethod
    def _check_format(cls, v: str) -> str:
        if "." not in v or v.startswith(".") or v.endswith("."):
            raise ValueError(f"references must be 'table.column', got {v!r}")
        return v

    @property
    def references_table(self) -> str:
        return self.references.split(".", 1)[0]

    @property
    def references_column(self) -> str:
        return self.references.split(".", 1)[1]


class ExplodeSpec(BaseModel):
    """Embedded object/array field -> rows in an existing child table."""

    target: str
    id_strategy: IdStrategy
    parent_fk: ForeignKeyRef
    fields: dict[str, FieldSpec] = Field(default_factory=dict)

    @field_validator("fields", mode="before")
    @classmethod
    def _coerce_fields(cls, v: Any) -> dict[str, Any]:
        return _normalize_field_map(v)


class JunctionSpec(BaseModel):
    """Scalar-ID array field -> rows in an existing many-to-many join table.

    Distinct from ExplodeSpec: a junction field has no independent payload
    fields of its own (it's just an array of foreign IDs), so there is no
    `fields` map — only the two FK sides.
    """

    target: str
    parent_fk: ForeignKeyRef
    child_fk: ForeignKeyRef


class UnmappedPolicy(BaseModel):
    """Explicit disposition for source fields not otherwise mapped.

    PRD §7: "every source field must resolve to a column, an explicit
    `drop`, or an explicit `jsonb` fallback — no silent drop."
    """

    drop: list[str] = Field(default_factory=list)
    jsonb: list[str] = Field(default_factory=list)

    @property
    def dispositioned(self) -> set[str]:
        return set(self.drop) | set(self.jsonb)

    @model_validator(mode="after")
    def _no_overlap(self) -> UnmappedPolicy:
        overlap = set(self.drop) & set(self.jsonb)
        if overlap:
            raise ValueError(f"fields listed in both drop and jsonb: {sorted(overlap)}")
        return self


class EntityMapping(BaseModel):
    source: str
    target: str
    id_strategy: IdStrategy
    fields: dict[str, FieldSpec] = Field(default_factory=dict)
    explode: dict[str, ExplodeSpec] = Field(default_factory=dict)
    junction: dict[str, JunctionSpec] = Field(default_factory=dict)
    unmapped: UnmappedPolicy = Field(default_factory=UnmappedPolicy)

    @field_validator("fields", mode="before")
    @classmethod
    def _coerce_fields(cls, v: Any) -> dict[str, Any]:
        return _normalize_field_map(v)

    @field_validator("unmapped", mode="before")
    @classmethod
    def _coerce_unmapped(cls, v: Any) -> Any:
        # Accept the PRD §12 shorthand `unmapped_fields: []` meaning "nothing
        # unmapped" as an alias for an empty UnmappedPolicy.
        if isinstance(v, list):
            if v:
                raise ValueError(
                    "`unmapped_fields` shorthand only accepts an empty list; use "
                    "`unmapped: {drop: [...], jsonb: [...]}` to give dispositions"
                )
            return {}
        return v

    def mapped_source_fields(self) -> set[str]:
        """All top-level source field names this entity gives a disposition
        to, one way or another (mapped, exploded, junctioned, or explicitly
        dropped/jsonb'd)."""
        return (
            set(self.fields.keys())
            | set(self.explode.keys())
            | set(self.junction.keys())
            | self.unmapped.dispositioned
            | {"_id"}  # always accounted for via id_strategy
        )


class MappingFile(BaseModel):
    entities: dict[str, EntityMapping]

    def entity_dependencies(self) -> dict[str, set[str]]:
        """entity name -> set of entity names it `lookup:`s against, at any
        level (own fields, explode sub-fields, or a junction's child_fk).

        This is deliberately derived from the mapping file, not from
        `PostgresSchema.dependency_edges()`: an entity's `explode`/
        `junction` children can depend on entities that entity's own target
        table has no direct FK to (e.g. `orders.items[].productId` looks up
        `products`, but the `orders` table itself has no FK to `products`
        — only `order_items` does). The load order that actually matters is
        "which entity's Mongo pass must run first", which only this graph
        answers correctly.
        """
        deps: dict[str, set[str]] = {name: set() for name in self.entities}
        for name, entity in self.entities.items():
            for fspec in entity.fields.values():
                if fspec.lookup:
                    deps[name].add(fspec.lookup)
            for exp in entity.explode.values():
                for fspec in exp.fields.values():
                    if fspec.lookup:
                        deps[name].add(fspec.lookup)
            for junc in entity.junction.values():
                if junc.child_fk.lookup:
                    deps[name].add(junc.child_fk.lookup)
        return deps

    def entity_load_order(self) -> list[str]:
        """Topological sort of entities by `lookup:` dependency (an entity
        that other entities look up must be loaded — and hence have rows in
        `_mongopg.id_map` — first). Raises CircularEntityDependencyError on
        a cycle; unlike PostgresSchema.load_order() there is no deferred-
        constraint escape hatch here, because id_map lookups happen at
        Mongo-read time in Python, not as a deferrable Postgres constraint.
        """
        deps = self.entity_dependencies()
        remaining = set(self.entities)
        order: list[str] = []
        while remaining:
            ready = sorted(n for n in remaining if not (deps[n] & remaining))
            if not ready:
                raise CircularEntityDependencyError(sorted(remaining))
            order.extend(ready)
            remaining -= set(ready)
        return order


class CircularEntityDependencyError(Exception):
    def __init__(self, entities: list[str]):
        self.entities = entities
        super().__init__(
            f"circular `lookup:` dependency among entities {entities} — "
            "an entity cannot (even indirectly, through explode/junction) depend on "
            "itself; break the cycle in the mapping file"
        )


class ValidationIssue(BaseModel):
    severity: str  # "error" | "warning"
    entity: str | None = None
    field: str | None = None
    message: str


def load_mapping_file(path: str | Path) -> MappingFile:
    path = Path(path)
    raw = yaml.safe_load(path.read_text())
    if not raw or "entities" not in raw:
        raise ValueError(f"{path}: mapping file must have a top-level `entities` key")
    return MappingFile.model_validate(raw)


def dump_mapping_file(mapping: MappingFile, path: str | Path) -> None:
    Path(path).write_text(
        yaml.safe_dump(
            mapping.model_dump(mode="json", exclude_none=True),
            sort_keys=False,
            default_flow_style=False,
        )
    )


def validate_structure(mapping: MappingFile) -> list[ValidationIssue]:
    """Structural checks that don't require live Mongo/Postgres connections:
    lookup targets exist, references point at declared entities, etc."""
    issues: list[ValidationIssue] = []
    entity_names = set(mapping.entities.keys())

    for name, entity in mapping.entities.items():
        for fname, fspec in entity.fields.items():
            if fspec.lookup and fspec.lookup not in entity_names:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        entity=name,
                        field=fname,
                        message=f"lookup: {fspec.lookup!r} is not a declared entity",
                    )
                )
        for ename, exp in entity.explode.items():
            for fname, fspec in exp.fields.items():
                if fspec.lookup and fspec.lookup not in entity_names:
                    issues.append(
                        ValidationIssue(
                            severity="error",
                            entity=name,
                            field=f"{ename}.{fname}",
                            message=f"lookup: {fspec.lookup!r} is not a declared entity",
                        )
                    )
        for jname, junc in entity.junction.items():
            if junc.child_fk.lookup and junc.child_fk.lookup not in entity_names:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        entity=name,
                        field=jname,
                        message=f"child_fk.lookup: {junc.child_fk.lookup!r} is not a declared entity",
                    )
                )
    return issues


def validate_against_mongo_schema(
    mapping: MappingFile, mongo_fields_by_entity: dict[str, set[str]]
) -> list[ValidationIssue]:
    """Enforce the P0 unmapped-field policy (PRD §7): every field observed on
    the live Mongo collection for an entity must have a disposition in the
    mapping. `mongo_fields_by_entity` maps entity name -> set of top-level
    field names seen during introspection (see introspect/mongo.py)."""
    issues: list[ValidationIssue] = []
    for name, entity in mapping.entities.items():
        observed = mongo_fields_by_entity.get(entity.source)
        if observed is None:
            issues.append(
                ValidationIssue(
                    severity="warning",
                    entity=name,
                    message=f"no introspected schema found for source collection {entity.source!r}; "
                    "skipping unmapped-field check for this entity",
                )
            )
            continue
        accounted = entity.mapped_source_fields()
        missing = observed - accounted
        for field in sorted(missing):
            issues.append(
                ValidationIssue(
                    severity="error",
                    entity=name,
                    field=field,
                    message=(
                        f"field {field!r} has no disposition — add it to `fields`, "
                        "`explode`, `junction`, or `unmapped.drop`/`unmapped.jsonb`"
                    ),
                )
            )
    return issues
