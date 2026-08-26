"""Unit-level check of the SQL _upsert_rows generates, without a live DB —
mirrors test_load_truncate.py's fake-connection pattern. The real behavior
(staging table + ON CONFLICT actually working, resume idempotency) is
verified live against Docker — see the session notes / README.
"""

from mongopg_migrate.migrate.load import _upsert_rows


class _FakeCopy:
    def __init__(self, log: list):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def write_row(self, row):
        self.log.append(("write_row", row))


class _FakeCursor:
    def __init__(self, log: list):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql: str, params=None):
        self.log.append(("execute", sql))

    def copy(self, sql: str):
        self.log.append(("copy", sql))
        return _FakeCopy(self.log)


class _FakeConn:
    def __init__(self):
        self.log: list = []

    def cursor(self):
        return _FakeCursor(self.log)

    def executed_sql(self) -> list[str]:
        return [entry[1] for entry in self.log if entry[0] == "execute"]


def test_empty_rows_is_a_noop():
    conn = _FakeConn()
    _upsert_rows(conn, "users", ["id", "email"], [], conflict_columns=["id"])
    assert conn.log == []


def test_main_table_upsert_does_update_on_conflict():
    conn = _FakeConn()
    _upsert_rows(
        conn,
        "users",
        ["id", "email", "display_name"],
        [("u1", "a@example.com", "Alex")],
        conflict_columns=["id"],
    )
    sql = conn.executed_sql()
    assert any(s.startswith('CREATE TEMP TABLE IF NOT EXISTS "__mongopg_stage_users"') for s in sql)
    assert any(s == 'TRUNCATE "__mongopg_stage_users"' for s in sql)
    insert_sql = next(s for s in sql if s.startswith("INSERT INTO"))
    assert 'ON CONFLICT ("id") DO UPDATE SET' in insert_sql
    assert '"email" = EXCLUDED."email"' in insert_sql
    assert '"display_name" = EXCLUDED."display_name"' in insert_sql
    assert '"id" = EXCLUDED."id"' not in insert_sql  # the conflict key itself is never in the SET list


def test_junction_table_upsert_does_nothing_on_conflict():
    conn = _FakeConn()
    _upsert_rows(
        conn,
        "order_tags",
        ["order_id", "tag_id"],
        [("o1", "t1")],
        conflict_columns=["order_id", "tag_id"],
    )
    insert_sql = next(s for s in conn.executed_sql() if s.startswith("INSERT INTO"))
    assert 'ON CONFLICT ("order_id", "tag_id") DO NOTHING' in insert_sql


def test_rows_are_copied_into_the_staging_table_not_the_real_table():
    conn = _FakeConn()
    _upsert_rows(conn, "users", ["id", "email"], [("u1", "a@example.com")], conflict_columns=["id"])
    copy_calls = [entry[1] for entry in conn.log if entry[0] == "copy"]
    assert len(copy_calls) == 1
    assert copy_calls[0].startswith('COPY "__mongopg_stage_users"')
