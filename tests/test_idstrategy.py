import uuid

import pytest

from mongopg_migrate.mapping.schema import IdStrategy, IdStrategyType
from mongopg_migrate.migrate.idstrategy import IdStrategyError, resolve_new_id


def test_objectid_to_uuid_is_deterministic():
    strategy = IdStrategy(type=IdStrategyType.OBJECTID_TO_UUID, source_field="_id")
    a = resolve_new_id(strategy, "64f1a2000000000000000001")
    b = resolve_new_id(strategy, "64f1a2000000000000000001")
    assert a.column_value == b.column_value
    assert isinstance(a.column_value, uuid.UUID)
    assert a.str_form == str(a.column_value)


def test_objectid_to_uuid_differs_for_different_sources():
    strategy = IdStrategy(type=IdStrategyType.OBJECTID_TO_UUID, source_field="_id")
    a = resolve_new_id(strategy, "64f1a2000000000000000001")
    b = resolve_new_id(strategy, "64f1a2000000000000000002")
    assert a.column_value != b.column_value


def test_passthrough_uses_string_form_of_source():
    strategy = IdStrategy(type=IdStrategyType.PASSTHROUGH, source_field="_id")
    result = resolve_new_id(strategy, "raw-source-id")
    assert result.column_value == "raw-source-id"
    assert result.str_form == "raw-source-id"


def test_uuid_generate_produces_unique_values():
    strategy = IdStrategy(type=IdStrategyType.UUID_GENERATE, source_field="_id")
    a = resolve_new_id(strategy, "x")
    b = resolve_new_id(strategy, "x")
    assert a.column_value != b.column_value  # non-deterministic by design


def test_int_sequence_requires_connection_and_default():
    strategy = IdStrategy(type=IdStrategyType.INT_SEQUENCE, source_field="_id")
    with pytest.raises(IdStrategyError):
        resolve_new_id(strategy, "1")


def test_serial_is_not_resolvable_here():
    strategy = IdStrategy(type=IdStrategyType.SERIAL)
    with pytest.raises(IdStrategyError):
        resolve_new_id(strategy, "1")
