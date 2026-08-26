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


# --- cast_* on a list/dict: found live re-verifying the HealthRail
# transcript's "arrays of plain scalars" note. int()/float() already raise
# on a list on their own, but bool()/str() don't: bool([1, 2, 3]) is True
# (any non-empty list is truthy) and str([1, 2, 3]) silently lands the
# Python repr '[1, 2, 3]' in a text column — same footgun class as the
# character-by-character scalar-iteration bug elsewhere in this tool.


def test_cast_bool_on_a_list_raises_instead_of_using_python_truthiness():
    with pytest.raises(TransformError, match="cannot cast a list"):
        apply_transform("cast_bool", [1, 2, 3])


def test_cast_bool_on_an_empty_list_still_raises():
    # Especially important: an empty list is falsy, so a silent bool()
    # coercion would produce a plausible-looking False instead of erroring.
    with pytest.raises(TransformError, match="cannot cast a list"):
        apply_transform("cast_bool", [])


def test_cast_text_on_a_list_raises_instead_of_stringifying_the_repr():
    with pytest.raises(TransformError, match="cannot cast a list"):
        apply_transform("cast_text", [1, 2, 3])


def test_cast_text_on_a_dict_raises():
    with pytest.raises(TransformError, match="cannot cast a dict"):
        apply_transform("cast_text", {"a": 1})


def test_cast_int_and_float_on_a_list_still_raise_with_the_new_guard():
    # int()/float() already raised before this fix — confirms the new
    # upfront guard doesn't change that, just makes bool/text consistent.
    with pytest.raises(TransformError, match="cannot cast a list"):
        apply_transform("cast_int", [1, 2, 3])
    with pytest.raises(TransformError, match="cannot cast a list"):
        apply_transform("cast_float", [1, 2, 3])


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


# --- enum: ---------------------------------------------------------------------------


def test_enum_maps_known_value():
    assert apply_transform('enum:{"1": "active", "2": "inactive"}', "1") == "active"
    assert apply_transform('enum:{"1": "active", "2": "inactive"}', 2) == "inactive"  # int key stringified


def test_enum_falls_back_to_wildcard():
    result = apply_transform('enum:{"1": "active", "*": "unknown"}', "99")
    assert result == "unknown"


def test_enum_raises_on_unlisted_value_with_no_wildcard():
    with pytest.raises(TransformError, match="no entry in the mapping"):
        apply_transform('enum:{"1": "active"}', "99")


def test_enum_raises_on_invalid_json():
    with pytest.raises(TransformError, match="invalid JSON"):
        apply_transform("enum:{not valid json", "1")


def test_enum_raises_when_mapping_is_not_an_object():
    with pytest.raises(TransformError, match="must be a JSON object"):
        apply_transform("enum:[1, 2, 3]", "1")


# --- split: ----------------------------------------------------------------------------


def test_split_turns_delimited_string_into_a_list():
    assert apply_transform("split:,", "red,blue,green") == ["red", "blue", "green"]


def test_split_supports_multi_character_delimiter():
    assert apply_transform("split:, ", "red, blue, green") == ["red", "blue", "green"]


def test_split_raises_on_empty_delimiter():
    with pytest.raises(TransformError, match="non-empty delimiter"):
        apply_transform("split:", "a,b")


def test_split_raises_on_non_string_input():
    with pytest.raises(TransformError, match="expected a string"):
        apply_transform("split:,", 42)
