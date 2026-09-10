"""`idmap.prefetch()` and `idmap.put_many()` — the id_map fast path.

`get()` was one network round trip per `lookup:` per row, which over a VPN
set the pace for the entire migration. `prefetch()` reads an entity's whole
map once and `get()` answers from memory; `put_many()` writes id_map rows one
statement per chunk instead of one per row.

Both shipped untested, and the risk is not a crash: a snapshot that goes
stale, or a miss that silently falls through, produces wrong foreign keys
that every later check agrees with. These pin the behaviours the correctness
argument in idmap.py actually rests on.
"""

from __future__ import annotations

import gc

import pytest

from mongopg_migrate.migrate import idmap


class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.last = ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.last = str(sql)
        self.conn.statements.append((str(sql), params))

    def fetchall(self):
        return self.conn.rows

    def fetchone(self):
        # prefetch() asks for the row count before deciding to cache anything,
        # so the fake has to answer that separately from a row lookup.
        if "count(*)" in self.last:
            return (len(self.conn.rows),)
        return self.conn.rows[0] if self.conn.rows else None


class _FakeConn:
    """Weak-referenceable, which the prefetch cache requires."""

    def __init__(self, rows=None):
        self.rows = rows or []
        self.statements: list[tuple] = []

    def cursor(self):
        return _FakeCursor(self)

    @property
    def selects(self):
        return [s for s, _ in self.statements if s.strip().upper().startswith("SELECT")]

    @property
    def inserts(self):
        return [s for s, _ in self.statements if "INSERT" in s.upper()]


@pytest.fixture(autouse=True)
def _clean_cache():
    idmap.clear_prefetch()
    yield
    idmap.clear_prefetch()


# ── prefetch ─────────────────────────────────────────────────────────────────


def test_prefetch_reports_how_many_rows_it_cached():
    conn = _FakeConn(rows=[("a", "1"), ("b", "2")])
    assert idmap.prefetch(conn, "users") == 2
    assert idmap.is_prefetched(conn, "users")


def test_prefetch_costs_a_fixed_number_of_statements_regardless_of_size():
    # A count to decide whether the entity fits in memory, then one fetch.
    # What matters is that it is O(1) per entity rather than O(1) per row.
    conn = _FakeConn(rows=[("a", "1")])
    idmap.prefetch(conn, "users")
    assert len(conn.selects) == 2
    assert "count(*)" in conn.selects[0]


def test_prefetch_can_skip_the_count_when_the_caller_opts_out_of_the_bound():
    conn = _FakeConn(rows=[("a", "1")])
    assert idmap.prefetch(conn, "users", max_rows=None) == 1
    assert len(conn.selects) == 1


def test_get_is_served_from_memory_after_a_prefetch():
    conn = _FakeConn(rows=[("a", "1")])
    idmap.prefetch(conn, "users")
    before = len(conn.statements)
    assert idmap.get(conn, "users", "a") == "1"
    assert len(conn.statements) == before, "get() queried despite a prefetched snapshot"


def test_a_miss_in_a_prefetched_snapshot_does_not_fall_through_to_a_query():
    """The whole point of the fast path.

    Load order guarantees the referenced entity is fully committed before
    anything looks it up, so a miss here is a real dangling reference. Falling
    back to a query would restore the per-row round trip for exactly the rows
    `on_missing` exists to handle — the pathological case, not the rare one.
    """
    conn = _FakeConn(rows=[("a", "1")])
    idmap.prefetch(conn, "users")
    before = len(conn.statements)
    assert idmap.get(conn, "users", "nope") is None
    assert len(conn.statements) == before


def test_an_unprefetched_entity_still_queries():
    # Opt-in by design: validate.py and dryrun.py have no batch context.
    conn = _FakeConn(rows=[("1",)])
    idmap.get(conn, "users", "a")
    assert len(conn.selects) == 1


def test_source_and_target_ids_are_normalised_to_strings():
    # Mongo hands back ObjectId/int; the map is keyed by str everywhere else.
    conn = _FakeConn(rows=[(123, 456)])
    idmap.prefetch(conn, "users")
    assert idmap.get(conn, "users", 123) == "456"
    assert idmap.get(conn, "users", "123") == "456"


def test_the_cache_is_keyed_per_entity():
    conn = _FakeConn(rows=[("a", "1")])
    idmap.prefetch(conn, "users")
    assert idmap.is_prefetched(conn, "users")
    assert not idmap.is_prefetched(conn, "orders")


def test_the_cache_is_keyed_per_schema():
    conn = _FakeConn(rows=[("a", "1")])
    idmap.prefetch(conn, "users", schema="_mongopg")
    assert not idmap.is_prefetched(conn, "users", schema="_mongopg_dryrun_x")


def test_the_cache_is_keyed_per_connection():
    a, b = _FakeConn(rows=[("a", "1")]), _FakeConn(rows=[])
    idmap.prefetch(a, "users")
    assert idmap.is_prefetched(a, "users")
    assert not idmap.is_prefetched(b, "users")


def test_reprefetching_replaces_the_snapshot():
    conn = _FakeConn(rows=[("a", "1")])
    idmap.prefetch(conn, "users")
    conn.rows = [("a", "999"), ("b", "2")]
    assert idmap.prefetch(conn, "users") == 2
    assert idmap.get(conn, "users", "a") == "999"


def test_clear_prefetch_scoped_to_one_connection():
    a, b = _FakeConn(rows=[("a", "1")]), _FakeConn(rows=[("a", "1")])
    idmap.prefetch(a, "users")
    idmap.prefetch(b, "users")
    idmap.clear_prefetch(a)
    assert not idmap.is_prefetched(a, "users")
    assert idmap.is_prefetched(b, "users")


def test_clear_prefetch_with_no_argument_clears_everything():
    a, b = _FakeConn(rows=[("a", "1")]), _FakeConn(rows=[("a", "1")])
    idmap.prefetch(a, "users")
    idmap.prefetch(b, "users")
    idmap.clear_prefetch()
    assert not idmap.is_prefetched(a, "users")
    assert not idmap.is_prefetched(b, "users")


def test_the_cache_does_not_keep_a_dead_connection_alive():
    # Keyed on a weak reference so cache lifetime follows the connection's,
    # rather than growing for the life of the process.
    conn = _FakeConn(rows=[("a", "1")])
    idmap.prefetch(conn, "users")
    del conn
    gc.collect()
    assert len(idmap._PREFETCHED) == 0


# ── write-through ────────────────────────────────────────────────────────────


def test_put_writes_through_to_a_live_snapshot():
    # Covers a read-back during an entity's own load: without this the
    # snapshot would answer None for a row that was just written.
    conn = _FakeConn(rows=[])
    idmap.prefetch(conn, "users")
    idmap.put(conn, "users", "new", "uuid-1")
    before = len(conn.statements)
    assert idmap.get(conn, "users", "new") == "uuid-1"
    assert len(conn.statements) == before


def test_put_many_writes_through_to_a_live_snapshot():
    conn = _FakeConn(rows=[])
    idmap.prefetch(conn, "users")
    idmap.put_many(conn, [("users", "a", "1"), ("users", "b", "2")])
    assert idmap.get(conn, "users", "a") == "1"
    assert idmap.get(conn, "users", "b") == "2"


def test_write_through_only_touches_the_matching_entity():
    conn = _FakeConn(rows=[])
    idmap.prefetch(conn, "users")
    idmap.put_many(conn, [("orders", "o1", "x")])
    assert not idmap.is_prefetched(conn, "orders")
    assert idmap.get(conn, "users", "o1") is None


# ── put_many ─────────────────────────────────────────────────────────────────


def test_put_many_issues_one_statement_for_a_whole_batch():
    conn = _FakeConn()
    idmap.put_many(conn, [("users", str(i), f"u{i}") for i in range(500)])
    assert len(conn.inserts) == 1, "put_many fell back to one statement per row"


def test_put_many_does_nothing_when_given_nothing():
    conn = _FakeConn()
    idmap.put_many(conn, [])
    assert conn.statements == []


def test_put_many_chunks_to_stay_under_the_placeholder_limit():
    # Postgres caps a statement at 65535 placeholders and each row costs 3.
    conn = _FakeConn()
    n = idmap._PUT_MANY_CHUNK * 2 + 10
    idmap.put_many(conn, [("users", str(i), f"u{i}") for i in range(n)])
    assert len(conn.inserts) == 3
    for _, params in conn.statements:
        assert len(params) <= 65535


def test_put_many_deduplicates_within_a_call_last_write_wins():
    """ON CONFLICT cannot see rows inserted by the same statement.

    A repeated (entity, source_id) inside one chunk would raise "command
    cannot affect row a second time" rather than behaving like the per-row
    loop it replaced. De-duplicating keeps the two interchangeable.
    """
    conn = _FakeConn()
    idmap.put_many(conn, [("users", "a", "first"), ("users", "a", "second")])
    _, params = conn.statements[0]
    assert params == ["users", "a", "second"]


def test_dedup_is_scoped_per_entity_not_per_source_id():
    conn = _FakeConn()
    idmap.put_many(conn, [("users", "a", "u"), ("orders", "a", "o")])
    _, params = conn.statements[0]
    assert len(params) == 6


# ── the memory bound ─────────────────────────────────────────────────────────
# The snapshot is ~200 bytes per row and an entity that looks up several
# others holds several at once, so an unbounded prefetch is a memory blowup
# waiting for a big enough source: ~1.2 GB at 5M rows, ~12 GB at 50M. It was
# never hit in development because the largest entity there was ~65k rows.


def test_an_entity_over_the_limit_is_not_cached():
    conn = _FakeConn(rows=[(str(i), f"u{i}") for i in range(10)])
    assert idmap.prefetch(conn, "users", max_rows=5) == -1
    assert not idmap.is_prefetched(conn, "users")


def test_an_oversized_entity_costs_only_the_count_query():
    # The rows must never be fetched to discover there are too many of them —
    # that is precisely the allocation being avoided.
    conn = _FakeConn(rows=[(str(i), f"u{i}") for i in range(10)])
    idmap.prefetch(conn, "users", max_rows=5)
    assert len(conn.selects) == 1
    assert "count(*)" in conn.selects[0]


def test_an_entity_exactly_at_the_limit_is_still_cached():
    conn = _FakeConn(rows=[(str(i), f"u{i}") for i in range(5)])
    assert idmap.prefetch(conn, "users", max_rows=5) == 5
    assert idmap.is_prefetched(conn, "users")


def test_the_default_limit_is_documented_and_finite():
    assert isinstance(idmap.DEFAULT_PREFETCH_MAX_ROWS, int)
    assert idmap.DEFAULT_PREFETCH_MAX_ROWS > 0


# ── the fallback cache ───────────────────────────────────────────────────────
# Bounded, and deliberately NOT authoritative: entries exist only for rows
# that were found, so a miss means "not cached", never "not present".


def test_a_repeated_lookup_is_served_from_the_fallback_cache():
    conn = _FakeConn(rows=[("uuid-1",)])
    assert idmap.get(conn, "users", "a") == "uuid-1"
    before = len(conn.selects)
    assert idmap.get(conn, "users", "a") == "uuid-1"
    assert len(conn.selects) == before, "the second lookup queried again"


def test_a_miss_is_never_cached_and_always_re_queries():
    """The correctness rule the whole fallback depends on.

    Caching absence would make `on_missing` fire on rows that are perfectly
    fine — the row simply had not been written when it was first asked for.
    """
    conn = _FakeConn(rows=[])
    assert idmap.get(conn, "users", "ghost") is None
    conn.rows = [("uuid-9",)]
    assert idmap.get(conn, "users", "ghost") == "uuid-9"


def test_the_fallback_cache_does_not_grow_without_bound():
    idmap.set_fallback_cache_size(4)
    try:
        conn = _FakeConn(rows=[("uuid-x",)])
        for i in range(50):
            idmap.get(conn, "users", f"k{i}")
        held = idmap._FALLBACK.get(conn, {}).get(("_mongopg", "users"), {})
        assert len(held) <= 4
    finally:
        idmap.set_fallback_cache_size(idmap.DEFAULT_FALLBACK_CACHE_SIZE)


def test_a_snapshot_still_wins_over_the_fallback_cache():
    conn = _FakeConn(rows=[("a", "snap")])
    idmap.prefetch(conn, "users")
    before = len(conn.selects)
    assert idmap.get(conn, "users", "a") == "snap"
    assert len(conn.selects) == before


def test_clear_prefetch_drops_the_fallback_cache_too():
    conn = _FakeConn(rows=[("uuid-1",)])
    idmap.get(conn, "users", "a")
    idmap.clear_prefetch(conn)
    assert conn not in idmap._FALLBACK
