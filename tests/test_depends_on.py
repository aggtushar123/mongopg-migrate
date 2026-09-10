"""`depends_on:` — a declared load-order edge the `lookup:` graph cannot infer.

`entity_dependencies()` derives ordering purely from `lookup:` targets. An
entity whose foreign key points at a parent it never names in a `lookup:` —
because the value is resolved through hand-seeded `external_entities` id_map
rows — produces no edge at all, floats to the front of the order, and COPYs
before its parent exists.

Untested when it shipped, which matters more here than usual: a bug in this
does not raise, it silently reorders a load. The validation paths are the
same story — a typo'd name that passes `validate_structure` restores exactly
the bug `depends_on` exists to fix, because an unknown name never intersects
the set of entities still to be ordered and so is trivially satisfied.
"""

from __future__ import annotations

import pytest

from mongopg_migrate.mapping.schema import (
    CircularEntityDependencyError,
    EntityMapping,
    FieldSpec,
    IdStrategy,
    IdStrategyType,
    MappingFile,
    validate_structure,
)


def _entity(source: str, *, fields=None, depends_on=None) -> EntityMapping:
    return EntityMapping(
        source=source,
        target=source,
        id_strategy=IdStrategy(type=IdStrategyType.OBJECTID_TO_UUID, source_field="_id"),
        fields=fields or {},
        depends_on=depends_on or [],
    )


def _mapping(**entities) -> MappingFile:
    return MappingFile(entities=entities)


# ── the ordering it exists to fix ────────────────────────────────────────────


def test_defaults_to_empty_so_existing_mappings_are_unaffected():
    assert _entity("orders").depends_on == []


def test_a_declared_edge_orders_the_dependency_first():
    # `documents` has no lookup: at all — exactly the case the graph cannot see.
    mapping = _mapping(
        documents=_entity("documents", depends_on=["hospitals"]),
        hospitals=_entity("hospitals"),
    )
    order = mapping.entity_load_order()
    assert order.index("hospitals") < order.index("documents")


def test_without_the_declaration_the_order_is_unconstrained():
    # Pins WHY the feature is needed: nothing here forces the right order.
    mapping = _mapping(documents=_entity("documents"), hospitals=_entity("hospitals"))
    assert mapping.entity_dependencies()["documents"] == set()


def test_it_appears_in_the_dependency_graph():
    mapping = _mapping(
        documents=_entity("documents", depends_on=["hospitals"]),
        hospitals=_entity("hospitals"),
    )
    assert mapping.entity_dependencies()["documents"] == {"hospitals"}


def test_it_unions_with_lookup_derived_edges_rather_than_replacing_them():
    mapping = _mapping(
        orders=_entity(
            "orders",
            fields={"userId": FieldSpec(target="user_id", lookup="users")},
            depends_on=["regions"],
        ),
        users=_entity("users"),
        regions=_entity("regions"),
    )
    assert mapping.entity_dependencies()["orders"] == {"users", "regions"}
    order = mapping.entity_load_order()
    assert order.index("users") < order.index("orders")
    assert order.index("regions") < order.index("orders")


def test_several_declared_edges_are_all_honoured():
    mapping = _mapping(
        c=_entity("c", depends_on=["a", "b"]),
        a=_entity("a"),
        b=_entity("b"),
    )
    order = mapping.entity_load_order()
    assert order.index("a") < order.index("c")
    assert order.index("b") < order.index("c")


def test_a_declared_cycle_is_still_a_cycle():
    mapping = _mapping(
        a=_entity("a", depends_on=["b"]),
        b=_entity("b", depends_on=["a"]),
    )
    with pytest.raises(CircularEntityDependencyError):
        mapping.entity_load_order()


def test_a_cycle_spanning_a_lookup_and_a_declared_edge_is_caught():
    mapping = _mapping(
        a=_entity("a", fields={"bId": FieldSpec(target="b_id", lookup="b")}),
        b=_entity("b", depends_on=["a"]),
    )
    with pytest.raises(CircularEntityDependencyError):
        mapping.entity_load_order()


# ── validation ───────────────────────────────────────────────────────────────


def test_an_unknown_entity_name_is_an_error():
    # Silently satisfied at ordering time, so without this check a typo
    # quietly restores the very bug depends_on was added to fix.
    mapping = _mapping(documents=_entity("documents", depends_on=["hosptials"]))
    issues = validate_structure(mapping)
    assert [i for i in issues if i.field == "depends_on" and i.severity == "error"]
    assert "hosptials" in issues[0].message


def test_the_error_lists_the_entities_that_do_exist():
    mapping = _mapping(
        documents=_entity("documents", depends_on=["nope"]),
        hospitals=_entity("hospitals"),
    )
    message = validate_structure(mapping)[0].message
    assert "hospitals" in message and "documents" in message


def test_an_external_entities_name_is_rejected_with_a_reason():
    # depends_on orders entities WITHIN one run; an already-migrated external
    # entity has nothing left to order.
    mapping = _mapping(documents=_entity("documents", depends_on=["from_another_run"]))
    assert "external_entities" in validate_structure(mapping)[0].message


def test_depending_on_itself_is_an_error():
    mapping = _mapping(documents=_entity("documents", depends_on=["documents"]))
    issues = [i for i in validate_structure(mapping) if i.field == "depends_on"]
    assert issues and "cannot" in issues[0].message


def test_a_valid_declaration_produces_no_issues():
    mapping = _mapping(
        documents=_entity("documents", depends_on=["hospitals"]),
        hospitals=_entity("hospitals"),
    )
    assert [i for i in validate_structure(mapping) if i.field == "depends_on"] == []


def test_it_is_not_treated_as_a_lookup_to_prefetch():
    """A declared edge resolves nothing.

    load.py bulk-loads an id_map for every entity in `lookup_entities()`
    before a load. Including a depends_on name there would read a map that is
    never consulted — wasted round trips against, potentially, a very large
    table.
    """
    entity = _entity(
        "orders",
        fields={"userId": FieldSpec(target="user_id", lookup="users")},
        depends_on=["regions"],
    )
    assert entity.lookup_entities() == {"users"}
