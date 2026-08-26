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


# --- int_sequence block reservation (fake connection, no real Postgres) --------------


class _FakeSequenceCursor:
    def __init__(self, counter_ref: list[int], log: list[str]):
        self.counter_ref = counter_ref
        self.log = log
        self._result: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.log.append(sql)
        if "generate_series" in sql:
            _seq_name, block_size = params
            start = self.counter_ref[0] + 1
            self.counter_ref[0] += block_size
            self._result = [(v,) for v in range(start, start + block_size)]
        else:
            self.counter_ref[0] += 1
            self._result = [(self.counter_ref[0],)]

    def fetchall(self):
        return self._result

    def fetchone(self):
        return self._result[0]


class _FakeConn:
    def __init__(self):
        self.counter = [0]
        self.log: list[str] = []

    def cursor(self):
        return _FakeSequenceCursor(self.counter, self.log)


_DEFAULT = "nextval('my_seq'::regclass)"


def test_int_sequence_without_id_buffer_does_one_nextval_round_trip_per_call():
    strategy = IdStrategy(type=IdStrategyType.INT_SEQUENCE, source_field="_id")
    conn = _FakeConn()
    a = resolve_new_id(strategy, "1", conn=conn, column_default=_DEFAULT)
    b = resolve_new_id(strategy, "2", conn=conn, column_default=_DEFAULT)
    assert (a.column_value, b.column_value) == (1, 2)
    assert len(conn.log) == 2
    assert all("generate_series" not in s for s in conn.log)


def test_int_sequence_with_id_buffer_reserves_one_block_for_many_calls():
    strategy = IdStrategy(type=IdStrategyType.INT_SEQUENCE, source_field="_id")
    conn = _FakeConn()
    buf: dict = {}
    values = [
        resolve_new_id(
            strategy, str(i), conn=conn, column_default=_DEFAULT, id_buffer=buf, reserve_block_size=5
        ).column_value
        for i in range(5)
    ]
    assert values == [1, 2, 3, 4, 5]
    assert len(conn.log) == 1  # one generate_series round trip served all 5
    assert "generate_series" in conn.log[0]


def test_int_sequence_with_id_buffer_refills_when_exhausted():
    strategy = IdStrategy(type=IdStrategyType.INT_SEQUENCE, source_field="_id")
    conn = _FakeConn()
    buf: dict = {}
    values = [
        resolve_new_id(
            strategy, str(i), conn=conn, column_default=_DEFAULT, id_buffer=buf, reserve_block_size=3
        ).column_value
        for i in range(7)
    ]
    assert values == [1, 2, 3, 4, 5, 6, 7]
    assert len(conn.log) == 3  # ceil(7/3)


def test_int_sequence_values_are_unique_and_str_form_matches():
    strategy = IdStrategy(type=IdStrategyType.INT_SEQUENCE, source_field="_id")
    conn = _FakeConn()
    buf: dict = {}
    results = [
        resolve_new_id(strategy, str(i), conn=conn, column_default=_DEFAULT, id_buffer=buf, reserve_block_size=4)
        for i in range(4)
    ]
    values = [r.column_value for r in results]
    assert len(set(values)) == 4
    assert all(r.str_form == str(r.column_value) for r in results)


def test_int_sequence_buffer_is_keyed_per_sequence_name():
    strategy = IdStrategy(type=IdStrategyType.INT_SEQUENCE, source_field="_id")
    conn = _FakeConn()
    buf: dict = {}
    resolve_new_id(
        strategy, "1", conn=conn, column_default="nextval('seq_a'::regclass)", id_buffer=buf, reserve_block_size=2
    )
    resolve_new_id(
        strategy, "2", conn=conn, column_default="nextval('seq_b'::regclass)", id_buffer=buf, reserve_block_size=2
    )
    assert set(buf.keys()) == {"seq_a", "seq_b"}
