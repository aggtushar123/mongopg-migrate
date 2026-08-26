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
    """Embedded object/array field -> rows in an existing child table.

    Can itself carry a nested `explode` — a second embedded array one level
    down (e.g. `hospitalDetails.facilities[].categoryParts[]` -> a
    `HospitalFacility` row per facility, each with its own `FacilityCategoryPart`
    child rows). A level with nested children needs its OWN id known *before*
    its row is COPYed, so that value can be threaded down as the nested
    level's `parent_fk` — that's the only reason `id_strategy` has to do real
    work here (a leaf level's PK can still just be `serial`, auto-assigned by
    Postgres at COPY time, exactly as before nesting existed). `serial` is
    rejected on a level that has nested children below it, since a SERIAL's
    actual assigned value isn't known until after INSERT and COPY has no
    RETURNING to recover it.
    """

    target: str
    id_strategy: IdStrategy
    parent_fk: ForeignKeyRef
    fields: dict[str, FieldSpec] = Field(default_factory=dict)
    explode: dict[str, ExplodeSpec] = Field(default_factory=dict)

    @field_validator("fields", mode="before")
    @classmethod
    def _coerce_fields(cls, v: Any) -> dict[str, Any]:
        return _normalize_field_map(v)

    @model_validator(mode="after")
    def _serial_id_cannot_have_nested_children(self) -> ExplodeSpec:
        if self.explode and self.id_strategy.type == IdStrategyType.SERIAL:
            raise ValueError(
                "explode level has nested `explode` children but id_strategy.type is 'serial' — "
                "a SERIAL value isn't known until after INSERT (COPY has no RETURNING), so it can't "
                "be threaded down to the nested children's parent_fk. Use objectid_to_uuid, "
                "uuid_generate, int_sequence, or passthrough instead, with a source_field that "
                "identifies this embedded item (e.g. its own `_id` if Mongo assigned one)."
            )
        return self


ExplodeSpec.model_rebuild()


class JunctionSpec(BaseModel):
    """Scalar-ID array field -> rows in an existing many-to-many join table.

    Distinct from ExplodeSpec: a junction field has no independent payload
    fields of its own (it's just an array of foreign IDs), so there is no
    `fields` map — only the two FK sides.
    """

    target: str
    parent_fk: ForeignKeyRef
    child_fk: ForeignKeyRef


class UnpivotItem(BaseModel):
    """One named source field an `unpivot` spec turns into a row."""

    source_field: str
    code: str
    transform: str | None = None


class UnpivotSpec(BaseModel):
    """N named, differently-shaped source fields -> N rows in an existing
    child table, each carrying a literal `code` identifying which source
    field it came from — the EAV/pivot-normalization pattern (e.g. a
    document with `pfAmount`/`finalBill`/`approvedCost` fields landing as
    three rows in a `booking_amounts` table, each tagged
    `PF_AMOUNT`/`FINAL_BILL`/`APPROVED_COST`). Distinct from both other
    multi-row constructs: `explode` takes one embedded *array* and repeats
    the same shape per item; `junction` takes one array of scalar FKs.
    Neither can express "several differently-named top-level fields, each
    becoming its own row" — that's what this is for. `skip_null` (default
    `True`) omits a row entirely when its source field is absent/null,
    rather than writing a row with a null `value_column`.
    """

    target: str
    parent_fk: ForeignKeyRef
    code_column: str
    value_column: str
    items: list[UnpivotItem]
    skip_null: bool = True

    @model_validator(mode="after")
    def _codes_and_source_fields_unique(self) -> UnpivotSpec:
        codes = [item.code for item in self.items]
        if len(codes) != len(set(codes)):
            raise ValueError(f"unpivot codes must be unique within one spec, got: {codes}")
        sources = [item.source_field for item in self.items]
        if len(sources) != len(set(sources)):
            raise ValueError(f"unpivot source_fields must be unique within one spec, got: {sources}")
        return self


class UnmappedPolicy(BaseModel):
    """Explicit disposition for source fields not otherwise mapped.

    PRD §7: "every source field must resolve to a column, an explicit
    `drop`, or an explicit `jsonb` fallback — no silent drop." `jsonb` is
    only a real landing, not a label, when `jsonb_column` names an actual
    jsonb/json column on the target table: migrate/load.py serializes every
    field in `jsonb` into one JSON object and writes it there. A mapping
    with `jsonb` fields but no `jsonb_column` is rejected at validation
    time — the earlier design let `jsonb` be indistinguishable from `drop`
    at load time, which silently broke the field-level "zero silent data
    loss" promise the disposition itself makes (PRD §9).
    """

    drop: list[str] = Field(default_factory=list)
    jsonb: list[str] = Field(default_factory=list)
    jsonb_column: str | None = None

    @property
    def dispositioned(self) -> set[str]:
        return set(self.drop) | set(self.jsonb)

    @model_validator(mode="after")
    def _no_overlap(self) -> UnmappedPolicy:
        overlap = set(self.drop) & set(self.jsonb)
        if overlap:
            raise ValueError(f"fields listed in both drop and jsonb: {sorted(overlap)}")
        return self

    @model_validator(mode="after")
    def _jsonb_column_matches_jsonb_fields(self) -> UnmappedPolicy:
        if self.jsonb and not self.jsonb_column:
            raise ValueError(
                "unmapped.jsonb is non-empty but unmapped.jsonb_column is not set — "
                "without a target column, these fields would silently be dropped at load "
                "time instead of landing anywhere (PRD §9 zero-silent-data-loss)"
            )
        if self.jsonb_column and not self.jsonb:
            raise ValueError("unmapped.jsonb_column is set but unmapped.jsonb is empty — nothing to land there")
        return self


class FilterSpec(BaseModel):
    """Restricts an entity to documents where `field == equals` — the
    mechanism for "multiple mappings filtered by discriminator" (PRD §7 P0,
    §6 step 3: a polymorphic collection like `payments` with a `type`
    discriminator can become two entities, `payments_card` and
    `payments_bank`, both with `source: payments` but different `filter`).
    Applied everywhere the entity's documents are read: migrate/load.py's
    Mongo query, dry-run Layer A's validation pass, and report/validate.py's
    count and sample diffs — an unfiltered `count_documents({})` against a
    split collection would count every other filtered entity's documents
    too, which is exactly the bug this closes."""

    field: str
    equals: str | int | bool


class EntityMapping(BaseModel):
    source: str
    target: str
    id_strategy: IdStrategy
    fields: dict[str, FieldSpec] = Field(default_factory=dict)
    explode: dict[str, ExplodeSpec] = Field(default_factory=dict)
    junction: dict[str, JunctionSpec] = Field(default_factory=dict)
    unpivot: dict[str, UnpivotSpec] = Field(default_factory=dict)
    unmapped: UnmappedPolicy = Field(default_factory=UnmappedPolicy)
    filter: FilterSpec | None = None

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
        to, one way or another (mapped, exploded, junctioned, unpivoted, or
        explicitly dropped/jsonb'd)."""
        unpivot_fields = {item.source_field for spec in self.unpivot.values() for item in spec.items}
        return (
            set(self.fields.keys())
            | set(self.explode.keys())
            | set(self.junction.keys())
            | unpivot_fields
            | self.unmapped.dispositioned
            | {"_id"}  # always accounted for via id_strategy
        )

    def mongo_filter(self) -> dict:
        """The base Mongo query this entity's documents must always match —
        `{}` unless `filter` restricts it to one discriminator value. Callers
        (migrate/load.py, migrate/dryrun.py, report/validate.py) combine this
        with their own per-call conditions (e.g. a resume cursor's `$gt`)."""
        if self.filter is None:
            return {}
        return {self.filter.field: self.filter.equals}


class MappingFile(BaseModel):
    entities: dict[str, EntityMapping]
    # Entity names this file's `lookup:`s are allowed to reference without
    # declaring them locally — PRD §12's own worked example assumes exactly
    # this ("assume `users` was migrated in an earlier run"): a later run
    # migrating only `orders` still needs `lookup: users` to resolve, via
    # `_mongopg.id_map` rows a previous run already wrote. Declaring the name
    # here (rather than silently allowing any unknown lookup target) keeps a
    # genuine typo in `lookup:` a hard error instead of a false "it's
    # external" pass.
    external_entities: list[str] = Field(default_factory=list)
    # Subset of `external_entities` whose id_map rows live in a *different*
    # Postgres database than this run's own --postgres-uri — the
    # microservices-split case: e.g. `bookings` targets the booking-service
    # database but needs `lookup: hospitals`, and `hospitals` was migrated
    # into a separate hospital-service database. Maps entity name -> the
    # NAME of an environment variable holding that database's connection
    # string (never the connection string itself — this is a mapping file,
    # typically checked into version control; a raw credential doesn't
    # belong in it, same reasoning as --llm-api-key-env). Resolved at
    # migrate/dry-run/validate time by migrate/load.py's
    # open_external_connections(). Entities not listed here but present in
    # `external_entities` are assumed to live in the *same* database as
    # this run and are checked against this run's own id_map, as before.
    external_databases: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _external_databases_subset_of_external_entities(self) -> MappingFile:
        unknown = set(self.external_databases) - set(self.external_entities)
        if unknown:
            raise ValueError(
                f"external_databases names entities not listed in external_entities: {sorted(unknown)} "
                "— add them there too"
            )
        return self

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
    known_names = entity_names | set(mapping.external_entities)

    def _check_lookup(name: str, field: str, lookup: str | None) -> None:
        if not lookup or lookup in known_names:
            return
        issues.append(
            ValidationIssue(
                severity="error",
                entity=name,
                field=field,
                message=f"lookup: {lookup!r} is not a declared entity and not listed in "
                "`external_entities` — if it was migrated in an earlier run, add it there",
            )
        )

    for name, entity in mapping.entities.items():
        for fname, fspec in entity.fields.items():
            _check_lookup(name, fname, fspec.lookup)
        for ename, exp in entity.explode.items():
            for fname, fspec in exp.fields.items():
                _check_lookup(name, f"{ename}.{fname}", fspec.lookup)
        for jname, junc in entity.junction.items():
            _check_lookup(name, jname, junc.child_fk.lookup)
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
        # Keyed by entity NAME, not entity.source — an entity can rename the
        # collection it maps (name != source, e.g. a discriminator-filtered
        # `payments_card` sourced from `payments`), and `entity.source` isn't
        # even unique across entities in that case. Regression: this used to
        # look up by `entity.source`, which silently degraded to the
        # "no introspected schema found" branch below whenever name != source
        # — the real check never ran, and the fixture never caught it because
        # every entity in it happens to have name == source.
        observed = mongo_fields_by_entity.get(name)
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
