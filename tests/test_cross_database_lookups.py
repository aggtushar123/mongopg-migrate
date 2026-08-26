"""Tests for cross-database external lookups (mapping.external_databases):
the microservices-split case where a `lookup:` target lives in a different
Postgres database entirely, not just a different mapping-file run against
the same database (that's plain `external_entities`, tested elsewhere).
"""

import pytest

from mongopg_migrate.introspect.postgres import ColumnInfo, PostgresSchema, TableSchema
from mongopg_migrate.mapping.schema import (
    EntityMapping,
    FieldSpec,
    IdStrategy,
    IdStrategyType,
    MappingFile,
)
from mongopg_migrate.migrate import idmap
from mongopg_migrate.migrate.load import (
    LoadError,
    _resolve_lookup,
    close_external_connections,
    open_external_connections,
)


def _entity(source="orders") -> EntityMapping:
    return EntityMapping(
        source=source,
        target=source,
        id_strategy=IdStrategy(type=IdStrategyType.OBJECTID_TO_UUID, source_field="_id"),
        fields={"hospitalId": FieldSpec(target="hospital_id", lookup="hospitals")},
    )


# --- schema validation -----------------------------------------------------------------


def test_external_databases_must_be_a_subset_of_external_entities():
    with pytest.raises(Exception, match="not listed in external_entities"):
        MappingFile(
            entities={"orders": _entity()},
            external_databases={"hospitals": "HOSPITAL_POSTGRES_URI"},
            # external_entities left empty — hospitals was never declared external at all
        )


def test_external_databases_valid_when_entity_is_also_declared_external():
    mapping = MappingFile(
        entities={"orders": _entity()},
        external_entities=["hospitals"],
        external_databases={"hospitals": "HOSPITAL_POSTGRES_URI"},
    )
    assert mapping.external_databases == {"hospitals": "HOSPITAL_POSTGRES_URI"}


def test_external_entities_without_external_databases_means_same_database():
    # The existing cross-RUN (not cross-database) case must still work
    # unchanged: declared external, no external_databases entry -> resolved
    # against this run's own id_map.
    mapping = MappingFile(entities={"orders": _entity()}, external_entities=["hospitals"])
    assert mapping.external_databases == {}


# --- open_external_connections / close_external_connections ---------------------------


class _FakeConn:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.closed = False

    def close(self):
        self.closed = True


@pytest.fixture
def fake_connect(monkeypatch):
    opened: list[str] = []

    def _fake_connect(dsn):
        opened.append(dsn)
        return _FakeConn(dsn)

    monkeypatch.setattr("mongopg_migrate.migrate.load.psycopg.connect", _fake_connect)
    return opened


def test_open_external_connections_reads_dsn_from_named_env_var(monkeypatch, fake_connect):
    monkeypatch.setenv("HOSPITAL_POSTGRES_URI", "postgresql://h/hospital_db")
    mapping = MappingFile(
        entities={"orders": _entity()},
        external_entities=["hospitals"],
        external_databases={"hospitals": "HOSPITAL_POSTGRES_URI"},
    )
    conns = open_external_connections(mapping)
    assert set(conns) == {"hospitals"}
    assert conns["hospitals"].dsn == "postgresql://h/hospital_db"
    assert fake_connect == ["postgresql://h/hospital_db"]


def test_open_external_connections_raises_when_env_var_unset(monkeypatch, fake_connect):
    monkeypatch.delenv("HOSPITAL_POSTGRES_URI", raising=False)
    mapping = MappingFile(
        entities={"orders": _entity()},
        external_entities=["hospitals"],
        external_databases={"hospitals": "HOSPITAL_POSTGRES_URI"},
    )
    with pytest.raises(LoadError, match="HOSPITAL_POSTGRES_URI"):
        open_external_connections(mapping)


def test_open_external_connections_shares_one_connection_per_distinct_dsn(monkeypatch, fake_connect):
    # Two external entities pointing at the same env var (same database)
    # must share one real connection, not open two.
    monkeypatch.setenv("SHARED_URI", "postgresql://h/shared_db")
    entities = {
        "orders": EntityMapping(
            source="orders",
            target="orders",
            id_strategy=IdStrategy(type=IdStrategyType.OBJECTID_TO_UUID, source_field="_id"),
            fields={
                "hospitalId": FieldSpec(target="hospital_id", lookup="hospitals"),
                "clinicId": FieldSpec(target="clinic_id", lookup="clinics"),
            },
        )
    }
    mapping = MappingFile(
        entities=entities,
        external_entities=["hospitals", "clinics"],
        external_databases={"hospitals": "SHARED_URI", "clinics": "SHARED_URI"},
    )
    conns = open_external_connections(mapping)
    assert conns["hospitals"] is conns["clinics"]
    assert len(fake_connect) == 1  # one real connection opened, not two


def test_open_external_connections_opens_separate_connections_for_different_dsns(monkeypatch, fake_connect):
    monkeypatch.setenv("HOSPITAL_URI", "postgresql://h/hospital_db")
    monkeypatch.setenv("KYC_URI", "postgresql://h/kyc_db")
    entities = {
        "orders": EntityMapping(
            source="orders",
            target="orders",
            id_strategy=IdStrategy(type=IdStrategyType.OBJECTID_TO_UUID, source_field="_id"),
            fields={
                "hospitalId": FieldSpec(target="hospital_id", lookup="hospitals"),
                "kycId": FieldSpec(target="kyc_id", lookup="kyc_docs"),
            },
        )
    }
    mapping = MappingFile(
        entities=entities,
        external_entities=["hospitals", "kyc_docs"],
        external_databases={"hospitals": "HOSPITAL_URI", "kyc_docs": "KYC_URI"},
    )
    conns = open_external_connections(mapping)
    assert conns["hospitals"] is not conns["kyc_docs"]
    assert len(fake_connect) == 2


def test_close_external_connections_closes_each_unique_connection_once(monkeypatch, fake_connect):
    monkeypatch.setenv("SHARED_URI", "postgresql://h/shared_db")
    entities = {
        "orders": EntityMapping(
            source="orders",
            target="orders",
            id_strategy=IdStrategy(type=IdStrategyType.OBJECTID_TO_UUID, source_field="_id"),
            fields={
                "hospitalId": FieldSpec(target="hospital_id", lookup="hospitals"),
                "clinicId": FieldSpec(target="clinic_id", lookup="clinics"),
            },
        )
    }
    mapping = MappingFile(
        entities=entities,
        external_entities=["hospitals", "clinics"],
        external_databases={"hospitals": "SHARED_URI", "clinics": "SHARED_URI"},
    )
    conns = open_external_connections(mapping)
    close_external_connections(conns)
    assert conns["hospitals"].closed
    assert conns["clinics"].closed  # same underlying object, but assert both keys report closed


def test_open_external_connections_empty_when_no_external_databases_declared():
    mapping = MappingFile(entities={"orders": _entity()})
    assert open_external_connections(mapping) == {}


# --- _resolve_lookup: external entity must never use this run's internal_schema -------
#
# Regression, found live: Layer B's disposable internal_schema (a random
# _mongopg_dryrun_* name, scoped to THIS run's own local bookkeeping) was
# leaking into external-entity lookups too, but the external database is a
# separate, independently migrated database whose id_map always lives under
# the real, default `_mongopg` schema regardless of what this run calls its
# own. Caught via a live two-database Docker test (see the session that
# added this), reproduced here as a fast, deterministic unit test.


class _FakeQueryCursor:
    def __init__(self, log: list[str], result):
        self.log = log
        self.result = result

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.log.append(sql)

    def fetchone(self):
        return self.result


class _FakeQueryConn:
    def __init__(self, result=None):
        self.log: list[str] = []
        self.result = result

    def cursor(self):
        return _FakeQueryCursor(self.log, self.result)


def _pg_schema_with_bookings() -> PostgresSchema:
    return PostgresSchema(
        tables={
            "bookings": TableSchema(
                name="bookings",
                columns={"hospital_id": ColumnInfo(name="hospital_id", data_type="uuid", is_nullable=False, default=None)},
                primary_key=["id"],
            ),
        }
    )


def test_resolve_lookup_uses_default_schema_for_external_entity_even_with_custom_internal_schema():
    local_conn = _FakeQueryConn(result=None)
    external_conn = _FakeQueryConn(result=("11111111-1111-1111-1111-111111111111",))

    _resolve_lookup(
        local_conn,
        "hospitals",
        "abc123",
        "bookings",
        "hospital_id",
        _pg_schema_with_bookings(),
        internal_schema="_mongopg_dryrun_deadbeef",  # Layer B's disposable name for THIS run
        external_conns={"hospitals": external_conn},
    )

    assert local_conn.log == []  # never queried — the external conn was used instead
    assert len(external_conn.log) == 1
    assert f'"{idmap.DEFAULT_SCHEMA_NAME}"."id_map"' in external_conn.log[0]
    assert "_mongopg_dryrun_deadbeef" not in external_conn.log[0]


def test_resolve_lookup_uses_internal_schema_for_a_same_database_entity():
    local_conn = _FakeQueryConn(result=("11111111-1111-1111-1111-111111111111",))

    _resolve_lookup(
        local_conn,
        "hospitals",
        "abc123",
        "bookings",
        "hospital_id",
        _pg_schema_with_bookings(),
        internal_schema="_mongopg_dryrun_deadbeef",
        external_conns=None,  # not an external entity in this run
    )

    assert len(local_conn.log) == 1
    assert '"_mongopg_dryrun_deadbeef"."id_map"' in local_conn.log[0]
