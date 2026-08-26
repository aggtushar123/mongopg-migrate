import datetime

import pytest

from mongopg_migrate.migrate.transform import (
    TransformError,
    apply_default,
    apply_transform,
    get_nested,
)


def test_get_nested_resolves_dotted_path():
    doc = {"shippingAddress": {"city": "Austin"}}
    assert get_nested(doc, "shippingAddress.city") == "Austin"


def test_get_nested_missing_segment_returns_none():
    assert get_nested({"a": {}}, "a.b.c") is None
    assert get_nested({}, "a") is None


def test_apply_transform_none_passthrough():
    assert apply_transform(None, "value") == "value"
    assert apply_transform("cast_int", None) is None


def test_cast_timestamptz_from_datetime_adds_utc_if_naive():
    naive = datetime.datetime(2026, 1, 4, 10, 0, 0)  # noqa: DTZ001 — testing the naive-input path on purpose
    result = apply_transform("cast_timestamptz", naive)
    assert result.tzinfo is not None


def test_cast_timestamptz_from_iso_string():
    result = apply_transform("cast_timestamptz", "2026-01-04T10:00:00")
    assert isinstance(result, datetime.datetime)
    assert result.tzinfo is not None


def test_cast_timestamptz_rejects_non_date():
    with pytest.raises(TransformError):
        apply_transform("cast_timestamptz", 12345)


def test_cast_int_and_float_and_text_and_bool():
    assert apply_transform("cast_int", "42") == 42
    assert apply_transform("cast_float", "3.14") == 3.14
    assert apply_transform("cast_text", 42) == "42"
    assert apply_transform("cast_bool", 1) is True


def test_unrecognized_transform_raises():
    with pytest.raises(TransformError):
        apply_transform("something_made_up", "x")


def test_cast_int_on_non_numeric_string_raises_transform_error_not_value_error():
    # Regression: cast_int/float/bool used to let a raw ValueError escape
    # instead of TransformError, so callers that catch TransformError
    # specifically (dry-run's fast pass, the real loader) never saw it.
    with pytest.raises(TransformError):
        apply_transform("cast_int", "not-a-number")


def test_json_extract_transform_is_a_passthrough_marker():
    assert apply_transform("json_extract:a.b", "already resolved") == "already resolved"


def test_apply_default_only_kicks_in_on_none():
    assert apply_default("default:0", None) == "0"
    assert apply_default("default:0", "present") == "present"
    assert apply_default(None, None) is None
    assert apply_default("cast_int", None) is None
