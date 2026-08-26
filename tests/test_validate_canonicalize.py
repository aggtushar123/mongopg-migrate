import datetime
import uuid
from decimal import Decimal

from mongopg_migrate.report.validate import _canonicalize, _row_hash, _values_equal


def test_float_and_decimal_of_same_value_canonicalize_equal():
    # The exact gotcha this exists for: Decimal('19.99') == 19.99 is not
    # reliably true due to binary float imprecision, but both should
    # canonicalize (and hash) identically.
    assert _canonicalize(19.99) == _canonicalize(Decimal("19.99"))


def test_int_and_float_of_same_value_canonicalize_equal():
    assert _canonicalize(5) == _canonicalize(5.0)


def test_uuid_canonicalizes_to_its_string_form():
    u = uuid.uuid4()
    assert _canonicalize(u) == str(u)


def test_datetime_with_different_but_equivalent_tzinfo_canonicalize_equal():
    utc_dt = datetime.datetime(2026, 1, 4, 10, 0, 0, tzinfo=datetime.UTC)
    offset_dt = utc_dt.astimezone(datetime.timezone(datetime.timedelta(hours=-5)))
    assert _canonicalize(utc_dt) == _canonicalize(offset_dt)


def test_none_and_bool_pass_through_unchanged():
    assert _canonicalize(None) is None
    assert _canonicalize(True) is True
    assert _canonicalize(False) is False


def test_row_hash_is_order_sensitive_and_value_sensitive():
    assert _row_hash([1, "a"]) != _row_hash(["a", 1])
    assert _row_hash([1, "a"]) == _row_hash([1.0, "a"])  # 1 vs 1.0 canonicalize equal
    assert _row_hash([1, "a"]) != _row_hash([2, "a"])


def test_row_hash_unaffected_by_numeric_column_scale():
    # Regression: repr(Decimal('19.99')) != repr(Decimal('19.990000')) even
    # though the values are == equal — a numeric(_, 6) column returning a
    # more-decimal-places Decimal than the recomputed float must not read
    # as a hash mismatch on its own.
    assert _row_hash([Decimal("19.99")]) == _row_hash([Decimal("19.990000")])
    assert _row_hash([19.99]) == _row_hash([Decimal("19.990000")])


def test_values_equal_uses_canonicalization():
    assert _values_equal(19.99, Decimal("19.99"))
    assert not _values_equal(19.99, Decimal("20.00"))


def test_lookup_missing_sentinel_never_equals_anything():
    from mongopg_migrate.report.validate import _LOOKUP_MISSING

    assert not _values_equal(_LOOKUP_MISSING, None)
    assert not _values_equal(_LOOKUP_MISSING, _LOOKUP_MISSING)


def test_row_hash_unaffected_by_jsonb_dict_key_order():
    # Regression: Postgres's jsonb storage does not preserve key insertion
    # order. == doesn't care, but repr() (what _row_hash hashes) does — a
    # value-identical jsonb blob read back with reordered keys must not
    # look like a mismatch purely from that reordering.
    recomputed = {"color": "red", "weight_kg": 1.5}
    actual_from_postgres = {"weight_kg": 1.5, "color": "red"}  # same content, different order
    assert _row_hash([recomputed]) == _row_hash([actual_from_postgres])


def test_row_hash_still_catches_a_real_jsonb_value_difference():
    a = {"color": "red", "weight_kg": 1.5}
    b = {"color": "blue", "weight_kg": 1.5}
    assert _row_hash([a]) != _row_hash([b])


def test_nested_dict_key_order_also_canonicalizes():
    a = {"outer": {"a": 1, "b": 2}}
    b = {"outer": {"b": 2, "a": 1}}
    assert _canonicalize(a) == _canonicalize(b)
    assert _row_hash([a]) == _row_hash([b])
