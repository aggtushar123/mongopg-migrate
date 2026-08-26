"""Regression tests for a real silent-corruption bug: `explode`/`junction`
iterated `doc.get(field) or []`, which looks like a safe "default to
empty" pattern but isn't — a non-empty string is truthy, so a scalar
string like "CARDIOLOGY" was iterated character-by-character (10 silent
rows, one per letter, no error) instead of being rejected. Found via a
real mapping that used `junction:` for what was actually a plain scalar
FK reference.
"""

import pytest

from mongopg_migrate.mapping.schema import EntityMapping, IdStrategy, IdStrategyType
from mongopg_migrate.migrate.dryrun import _as_array, _validate_array_shapes
from mongopg_migrate.migrate.load import LoadError, _require_array


def test_char_by_char_iteration_is_the_real_python_footgun():
    # The bug this whole file exists to prevent, demonstrated directly:
    # "or []" does not save you from a truthy scalar.
    doc = {"department": "CARDIOLOGY"}
    assert list(doc.get("department") or []) == list("CARDIOLOGY")
    assert len(doc.get("department") or []) == 10


# --- migrate/load.py: _require_array (raises) -----------------------------------------


def test_require_array_returns_empty_list_for_missing_field():
    assert _require_array({}, "tags", context="widgets") == []


def test_require_array_returns_empty_list_for_none():
    assert _require_array({"tags": None}, "tags", context="widgets") == []


def test_require_array_passes_through_a_real_list():
    assert _require_array({"tags": ["a", "b"]}, "tags", context="widgets") == ["a", "b"]


def test_require_array_raises_on_a_scalar_string():
    with pytest.raises(LoadError, match="is not an array"):
        _require_array({"department": "CARDIOLOGY"}, "department", context="widgets")


def test_require_array_error_names_the_actual_type_and_value():
    with pytest.raises(LoadError, match="str.*CARDIOLOGY"):
        _require_array({"department": "CARDIOLOGY"}, "department", context="widgets")


def test_require_array_raises_on_a_dict():
    with pytest.raises(LoadError, match="is not an array"):
        _require_array({"tags": {"a": 1}}, "tags", context="widgets")


def test_require_array_raises_on_an_int():
    with pytest.raises(LoadError, match="is not an array"):
        _require_array({"count": 42}, "count", context="widgets")


def test_require_array_error_suggests_the_fix():
    with pytest.raises(LoadError, match=r"`fields:` with `lookup:`"):
        _require_array({"department": "CARDIOLOGY"}, "department", context="widgets")


# --- migrate/dryrun.py: _as_array (permissive) + _validate_array_shapes (reports) -----


def test_as_array_returns_empty_for_a_scalar_string():
    assert _as_array("CARDIOLOGY") == []


def test_as_array_returns_empty_for_none():
    assert _as_array(None) == []


def test_as_array_passes_through_a_real_list():
    assert _as_array(["a", "b"]) == ["a", "b"]


def _entity_with_junction() -> EntityMapping:
    from mongopg_migrate.mapping.schema import ForeignKeyRef, JunctionSpec

    return EntityMapping(
        source="users",
        target="users",
        id_strategy=IdStrategy(type=IdStrategyType.OBJECTID_TO_UUID, source_field="_id"),
        junction={
            "department": JunctionSpec(
                target="user_department",
                parent_fk=ForeignKeyRef(target_field="user_id", references="users.id"),
                child_fk=ForeignKeyRef(target_field="department_id", references="departments.id", lookup="departments"),
            )
        },
    )


def test_validate_array_shapes_flags_a_scalar_junction_field():
    entity = _entity_with_junction()
    violations = _validate_array_shapes({"department": "CARDIOLOGY"}, entity, "users")
    assert len(violations) == 1
    assert violations[0].field == "department"
    assert "not an array" in violations[0].message


def test_validate_array_shapes_is_clean_for_a_real_array():
    entity = _entity_with_junction()
    violations = _validate_array_shapes({"department": ["CARDIOLOGY_ID"]}, entity, "users")
    assert violations == []


def test_validate_array_shapes_is_clean_when_field_is_absent():
    entity = _entity_with_junction()
    assert _validate_array_shapes({}, entity, "users") == []


def _entity_with_explode() -> EntityMapping:
    from mongopg_migrate.mapping.schema import ExplodeSpec, ForeignKeyRef

    return EntityMapping(
        source="orders",
        target="orders",
        id_strategy=IdStrategy(type=IdStrategyType.OBJECTID_TO_UUID, source_field="_id"),
        explode={
            "items": ExplodeSpec(
                target="order_items",
                id_strategy=IdStrategy(type=IdStrategyType.SERIAL),
                parent_fk=ForeignKeyRef(target_field="order_id", references="orders.id"),
            )
        },
    )


def test_validate_array_shapes_flags_a_scalar_explode_field():
    entity = _entity_with_explode()
    violations = _validate_array_shapes({"items": "not-a-list"}, entity, "orders")
    assert len(violations) == 1
    assert violations[0].field == "items"
    assert "silently iterate wrong" in violations[0].message
