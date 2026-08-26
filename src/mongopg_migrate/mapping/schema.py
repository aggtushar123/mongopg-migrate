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
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class OnMissing(str, Enum):
    """Policy for a `lookup:` that resolves to no `_mongopg.id_map` row — a
    *dangling reference* (the source field points at a real value, but the
    entity it should resolve against has no matching row, e.g. it was
    deleted, or was never migrated). Distinct from the field being absent/
    null on the source document, which was already handled fine before this
    existed (NULL in, NULL out, subject to the target column's NOT NULL
    check) — a dangling reference is a data-quality problem the tool
    previously had no policy for at all: it just hard-failed the whole
    batch (`LoadError`), unconditionally, with no way to say "I know about
    this, here's what to do."

    - ERROR (default): unchanged prior behavior — hard-fail migrate,
      report a violation at dry-run. Never silently proceed past a
      dangling reference unless a policy explicitly says to.
    - NULL: write NULL for this field. Still subject to the target
      column's NOT NULL check same as any other null value — this can't
      rescue a NOT NULL column, and shouldn't: that's a real schema
      mismatch, not a policy decision. Every occurrence is counted and
      reported (migrate output, dry-run, and `validate`'s on_missing
      section) — a silent null-ing here would be the exact bug class this
      tool exists to prevent (counts match, data is quietly wrong).
    - SKIP_ROW: drop the row this field's value belongs to, rather than
      writing it at all. For a top-level entity field, that's the whole
      document (and everything derived from it — explode/junction/unpivot
      children, its id_map entry). For an `explode` field, that's just the
      one array item, not its parent or siblings. Also counted and
      reported, same as NULL.
    """

    ERROR = "error"
    NULL = "null"
    SKIP_ROW = "skip_row"


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
    # See OnMissing — only meaningful (and only accepted) alongside `lookup`;
    # a plain field has no "dangling reference" concept to have a policy on.
    on_missing: OnMissing = OnMissing.ERROR

    @classmethod
    def coerce(cls, value: Any) -> FieldSpec:
        if isinstance(value, str):
            return cls(target=value)
        if isinstance(value, FieldSpec):
            return value
        return cls(**value)

    @field_validator("on_missing", mode="before")
    @classmethod
    def _coerce_bare_yaml_null(cls, v: Any) -> Any:
        # `on_missing: null` (no quotes) is the single most natural way to
        # write this policy — and YAML parses a bare, unquoted `null` as
        # Python None, not the string "null". Without this, that entirely
        # reasonable spelling would fail with a confusing enum-validation
        # error instead of doing what it obviously means. `on_missing: None`
        # meaning "don't set on_missing" isn't a real use case — omitting
        # the key entirely already means that (defaults to `error`) — so
        # there's no ambiguity being papered over here.
        return "null" if v is None else v

    @model_validator(mode="after")
    def _on_missing_requires_lookup(self) -> FieldSpec:
        if self.on_missing != OnMissing.ERROR and not self.lookup:
            raise ValueError(
                f"on_missing={self.on_missing.value!r} only applies to a `lookup:` field — a dangling "
                "reference is specifically 'the lookup target has no matching id_map row', which only "
                "means something when there's a lookup to dangle. Add `lookup:` or drop `on_missing:`."
            )
        return self


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

    Also usable on an `ExplodeSpec` (not just the top-level `EntityMapping`)
    for the exact same reason, one level down: a field inside an exploded
    array item with no disposition was previously dropped with zero
    warning — introspection already tracks nested paths like
    `items[].discount` (CollectionSchema._walk_document), but nothing
    checked them against the mapping until this existed.
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
    # Same P0 unmapped-field policy as EntityMapping's own `unmapped:`, one
    # level down — a field inside this exploded array item with no
    # disposition is otherwise silently dropped, exactly the bug class the
    # top-level policy exists to prevent.
    unmapped: UnmappedPolicy = Field(default_factory=UnmappedPolicy)

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

    def mapped_item_fields(self) -> set[str]:
        """All field names *within one item of this exploded array* that
        have a disposition — mirrors EntityMapping.mapped_source_fields()
        one level down. `id_strategy.source_field` is auto-accounted for
        the same reason `_id` is at the top level: only relevant (and only
        required) when this level has nested children of its own, in which
        case that field identifies the item rather than needing its own
        `fields:`/`unmapped:` disposition."""
        accounted = set(self.fields.keys()) | set(self.explode.keys()) | self.unmapped.dispositioned
        if self.explode and self.id_strategy.source_field:
            accounted.add(self.id_strategy.source_field)
        return accounted


ExplodeSpec.model_rebuild()


class JunctionSpec(BaseModel):
    """Scalar-ID array field -> rows in an existing many-to-many join table.

    Distinct from ExplodeSpec: a junction field has no independent payload
    fields of its own (it's just an array of foreign IDs), so there is no
    `fields` map — only the two FK sides.

    `on_missing` covers a dangling `child_fk.lookup` (see OnMissing) — but
    only `error`/`skip_row` are valid here, not `null`: a junction row's
    child_fk *is* half the row's identity (parent_fk, child_fk together are
    the natural key), so there's no such thing as "the row, but with a null
    identity" the way there is for a normal field. `skip_row` here means
    dropping just that one junction row, not the parent entity's row.
    """

    target: str
    parent_fk: ForeignKeyRef
    child_fk: ForeignKeyRef
    on_missing: Literal[OnMissing.ERROR, OnMissing.SKIP_ROW] = OnMissing.ERROR

    @model_validator(mode="after")
    def _on_missing_requires_child_lookup(self) -> JunctionSpec:
        if self.on_missing != OnMissing.ERROR and not self.child_fk.lookup:
            raise ValueError(
                f"on_missing={self.on_missing.value!r} only applies when `child_fk.lookup` is set — "
                "a dangling reference only means something when child_fk resolves via a lookup."
            )
        return self


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
    # Collections that exist in the source database and are *deliberately*
    # not migrated by this mapping file — e.g. `auditLogs`, a scratch
    # collection, or one already covered by a separate run. The
    # unmapped-field policy (PRD §7 P0) only ever looks inside an entity
    # that's already in `entities:`; a collection simply absent from the
    # file has never been mentioned by any command, at any severity —
    # "deliberately not migrating this" and "forgot this collection
    # existed" were genuinely indistinguishable. `validate_collection_
    # coverage` (below) is what actually checks this against a live
    # database; this list is how a collection earns its way out of that
    # warning, on purpose, in a form that's checked into version control
    # alongside the rest of the decision.
    excluded_collections: list[str] = Field(default_factory=list)

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


class _DuplicateKeyCheckingLoader(yaml.SafeLoader):
    """`yaml.safe_load`'s default behavior for a duplicate mapping key is
    silent last-one-wins — found live, writing a mapping with the same
    source field key twice under `fields:` (once as a resolved lookup,
    once as a raw passthrough): the first entry vanished with no warning
    at all. Same footgun class as the scalar-iteration and bare-`null`
    bugs already fixed elsewhere in this tool — the natural, deliberate-
    looking way to write it does something silently wrong. Applies
    anywhere in the mapping file a YAML mapping key could collide:
    entity names, field keys, explode/junction/unpivot names, and so on.
    """

    def construct_mapping(self, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    None,
                    None,
                    f"duplicate key {key!r} — only the last occurrence would otherwise silently "
                    "survive, dropping every earlier one with no warning",
                    key_node.start_mark,
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def load_mapping_file(path: str | Path) -> MappingFile:
    path = Path(path)
    try:
        raw = yaml.load(path.read_text(), Loader=_DuplicateKeyCheckingLoader)
    except yaml.constructor.ConstructorError as e:
        raise ValueError(f"{path}: {e}") from e
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


def _field_names_at_path(all_field_paths: set[str], path_prefix: str) -> set[str]:
    """Given the full flattened field-path set introspection produces (e.g.
    {"items[].productId", "items[].qty", "items[].meta.color", ...}) and a
    prefix like "items[]." (the exact prefix CollectionSchema._walk_document
    uses when it recurses into an array-of-objects field), returns just the
    immediate field names one level below that prefix — "productId", "qty",
    "meta" for the example above, not "meta.color" — mirroring what
    CollectionSchema.top_level_field_names() does for the very top of the
    document."""
    names: set[str] = set()
    for path in all_field_paths:
        if not path.startswith(path_prefix):
            continue
        rest = path[len(path_prefix) :]
        cut = len(rest)
        if "." in rest:
            cut = min(cut, rest.index("."))
        if "[]" in rest:
            cut = min(cut, rest.index("[]"))
        names.add(rest[:cut])
    return names


def _validate_explode_field_coverage(
    exp: ExplodeSpec, all_field_paths: set[str], *, path_prefix: str, context: str
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    observed = _field_names_at_path(all_field_paths, path_prefix)
    missing = observed - exp.mapped_item_fields()
    for field in sorted(missing):
        issues.append(
            ValidationIssue(
                severity="error",
                entity=context,
                field=field,
                message=(
                    f"field {field!r} inside the exploded array at {path_prefix.rstrip('.')!r} has no "
                    "disposition — add it to this explode level's `fields`, a nested `explode`, or "
                    "`unmapped.drop`/`unmapped.jsonb`"
                ),
            )
        )
    for nested_ename, nested_exp in exp.explode.items():
        issues.extend(
            _validate_explode_field_coverage(
                nested_exp, all_field_paths,
                path_prefix=f"{path_prefix}{nested_ename}[].",
                context=f"{context}.{nested_ename}",
            )
        )
    return issues


def validate_explode_field_coverage(
    mapping: MappingFile, all_field_paths_by_entity: dict[str, set[str]]
) -> list[ValidationIssue]:
    """Extends the P0 unmapped-field policy (validate_against_mongo_schema,
    above) one level down: a field *inside* an exploded array item with no
    disposition was previously silently dropped, with no warning anywhere
    — introspection has always tracked these nested paths (e.g.
    "items[].discount"), nothing ever checked them against the mapping
    until this existed. `all_field_paths_by_entity` maps entity name -> the
    *full* set of dotted/bracketed field paths introspection observed
    (CollectionSchema.fields.keys() — not just top_level_field_names()),
    keyed the same way and for the same reason as
    validate_against_mongo_schema's own parameter."""
    issues: list[ValidationIssue] = []
    for name, entity in mapping.entities.items():
        if not entity.explode:
            continue
        all_paths = all_field_paths_by_entity.get(name)
        if all_paths is None:
            continue  # validate_against_mongo_schema already warns about this entity; don't warn twice
        for ename, exp in entity.explode.items():
            issues.extend(
                _validate_explode_field_coverage(
                    exp, all_paths, path_prefix=f"{ename}[].", context=f"{name}.{ename}"
                )
            )
    return issues


def validate_collection_coverage(mapping: MappingFile, all_collection_names: set[str]) -> list[ValidationIssue]:
    """Every collection that actually exists in the source database should
    have a deliberate disposition — mapped as some entity's `source:`, or
    named in `excluded_collections` — not just be silently absent from the
    mapping file. Unlike the P0 unmapped-*field* policy above (which is a
    hard error: a field inside an already-migrated entity has no excuse to
    be unaccounted for), this is a warning: a real Mongo database can hold
    plenty of collections genuinely irrelevant to this migration (system/
    internal collections, unrelated app data), and demanding every one be
    explicitly listed would be pure noise for the common case. The point
    isn't to force zero unmapped collections — it's to make "deliberately
    not migrating this" and "forgot this collection existed" distinguishable,
    which they weren't at all before this existed."""
    accounted = {entity.source for entity in mapping.entities.values()} | set(mapping.excluded_collections)
    missing = all_collection_names - accounted
    return [
        ValidationIssue(
            severity="warning",
            entity=None,
            message=(
                f"collection {name!r} exists in the source database but is not mapped by any "
                "entity and not listed in `excluded_collections` — deliberately not migrating "
                "it? Add it to `excluded_collections` to record that on purpose; otherwise map it."
            ),
        )
        for name in sorted(missing)
    ]
