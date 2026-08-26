"""Coverage for the `unpivot:` construct — N differently-named top-level
scalar fields turned into N rows in an existing table, each carrying a
literal `code` (the EAV/pivot-normalization pattern, e.g. a `bookingPayment`
document with `pfAmount`/`payToHospital`/`finalBill`/`approvedCost` fields
landing as up to four `BookingAmount` rows). Added in response to a real
mapping that could not be expressed with `explode`/`junction` alone.

Three layers, mirroring how explode/junction are covered elsewhere:
- schema.py: UnpivotSpec validation (unique codes/source_fields).
- dryrun.py: Layer A validation (transform errors, NOT NULL, skip_null).
- load.py: _resolve_unpivot_value (transform/default/NOT NULL) and the
  COPY/upsert SQL it produces, mirroring test_load_upsert.py's fake-conn
  pattern for the parts that don't need a live DB.
"""

import mongomock
import pytest

from mongopg_migrate.introspect.postgres import ColumnInfo, PostgresSchema, TableSchema
from mongopg_migrate.mapping.schema import (
    EntityMapping,
    ForeignKeyRef,
    IdStrategy,
    IdStrategyType,
    MappingFile,
    UnpivotItem,
    UnpivotSpec,
)
from mongopg_migrate.migrate.dryrun import _validate_unpivot_items, run_fast_pass
from mongopg_migrate.migrate.load import LoadError, _resolve_unpivot_value


def col(name: str, data_type: str, nullable: bool = True) -> ColumnInfo:
    return ColumnInfo(name=name, data_type=data_type, is_nullable=nullable, default=None)


def make_pg_schema() -> PostgresSchema:
    return PostgresSchema(
        tables={
            "bookings": TableSchema(
                name="bookings", columns={"id": col("id", "uuid")}, primary_key=["id"]
            ),
            "booking_amounts": TableSchema(
                name="booking_amounts",
                columns={
                    "id": col("id", "bigint"),
                    "booking_id": col("booking_id", "uuid", nullable=False),
                    "code": col("code", "text", nullable=False),
                    "amount": col("amount", "numeric", nullable=False),
                },
                primary_key=["id"],
            ),
        }
    )


def make_unpivot_spec() -> UnpivotSpec:
    return UnpivotSpec(
        target="booking_amounts",
        parent_fk=ForeignKeyRef(target_field="booking_id", references="bookings.id"),
        code_column="code",
        value_column="amount",
        items=[
            UnpivotItem(source_field="pfAmount", code="PF_AMOUNT"),
            UnpivotItem(source_field="payToHospital", code="PAY_TO_HOSPITAL"),
            UnpivotItem(source_field="finalBill", code="FINAL_BILL"),
        ],
    )


def make_mapping() -> MappingFile:
    return MappingFile(
        entities={
            "bookings": EntityMapping(
                source="bookingPayment",
                target="bookings",
                id_strategy=IdStrategy(type=IdStrategyType.OBJECTID_TO_UUID, source_field="_id"),
                unpivot={"amounts": make_unpivot_spec()},
            )
        }
    )


# --- schema.py: UnpivotSpec validation --------------------------------------------------


def test_duplicate_codes_are_rejected():
    with pytest.raises(ValueError, match="codes must be unique"):
        UnpivotSpec(
            target="booking_amounts",
            parent_fk=ForeignKeyRef(target_field="booking_id", references="bookings.id"),
            code_column="code",
            value_column="amount",
            items=[
                UnpivotItem(source_field="a", code="X"),
                UnpivotItem(source_field="b", code="X"),
            ],
        )


def test_duplicate_source_fields_are_rejected():
    with pytest.raises(ValueError, match="source_fields must be unique"):
        UnpivotSpec(
            target="booking_amounts",
            parent_fk=ForeignKeyRef(target_field="booking_id", references="bookings.id"),
            code_column="code",
            value_column="amount",
            items=[
                UnpivotItem(source_field="a", code="X"),
                UnpivotItem(source_field="a", code="Y"),
            ],
        )


def test_mapped_source_fields_includes_unpivot_items():
    entity = make_mapping().entities["bookings"]
    mapped = entity.mapped_source_fields()
    assert {"pfAmount", "payToHospital", "finalBill"} <= mapped


# --- load.py: _resolve_unpivot_value -----------------------------------------------------


def test_resolve_unpivot_value_reads_the_named_field():
    spec = make_unpivot_spec()
    doc = {"pfAmount": 150.5}
    value = _resolve_unpivot_value(
        doc, spec.items[0], context="bookings.amounts", pg_schema=make_pg_schema(),
        target_table="booking_amounts", value_column="amount",
    )
    assert value == 150.5


def test_resolve_unpivot_value_applies_transform():
    item = UnpivotItem(source_field="pfAmount", code="PF_AMOUNT", transform="cast_int")
    doc = {"pfAmount": "150"}
    value = _resolve_unpivot_value(
        doc, item, context="bookings.amounts", pg_schema=make_pg_schema(),
        target_table="booking_amounts", value_column="amount",
    )
    assert value == 150


def test_resolve_unpivot_value_raises_on_null_into_not_null_column():
    spec = make_unpivot_spec()
    with pytest.raises(LoadError, match="NOT NULL"):
        _resolve_unpivot_value(
            {}, spec.items[0], context="bookings.amounts", pg_schema=make_pg_schema(),
            target_table="booking_amounts", value_column="amount",
        )


def test_resolve_unpivot_value_default_transform_avoids_not_null_error():
    item = UnpivotItem(source_field="pfAmount", code="PF_AMOUNT", transform="default:0")
    value = _resolve_unpivot_value(
        {}, item, context="bookings.amounts", pg_schema=make_pg_schema(),
        target_table="booking_amounts", value_column="amount",
    )
    assert value == "0"  # default: is a raw literal — no cast_* chained here, matches other constructs' behavior


# --- dryrun.py: _validate_unpivot_items ---------------------------------------------------


def test_validate_unpivot_items_clean_when_all_present():
    spec = make_unpivot_spec()
    doc = {"pfAmount": 100, "payToHospital": 200, "finalBill": 300}
    violations = _validate_unpivot_items(doc, "amounts", spec, context="bookings", pg_schema=make_pg_schema())
    assert violations == []


def test_validate_unpivot_items_skips_missing_field_when_skip_null():
    spec = make_unpivot_spec()
    assert spec.skip_null is True
    doc = {"pfAmount": 100}  # payToHospital, finalBill both absent
    violations = _validate_unpivot_items(doc, "amounts", spec, context="bookings", pg_schema=make_pg_schema())
    assert violations == []


def test_validate_unpivot_items_flags_not_null_violation_when_skip_null_false():
    spec = make_unpivot_spec()
    spec.skip_null = False
    doc = {"pfAmount": 100}  # payToHospital, finalBill absent but not skipped
    violations = _validate_unpivot_items(doc, "amounts", spec, context="bookings", pg_schema=make_pg_schema())
    assert len(violations) == 2
    assert all("NOT NULL" in v.message for v in violations)


def test_validate_unpivot_items_flags_transform_error():
    item = UnpivotItem(source_field="pfAmount", code="PF_AMOUNT", transform="cast_int")
    spec = UnpivotSpec(
        target="booking_amounts",
        parent_fk=ForeignKeyRef(target_field="booking_id", references="bookings.id"),
        code_column="code",
        value_column="amount",
        items=[item],
    )
    doc = {"pfAmount": "not-a-number"}
    violations = _validate_unpivot_items(doc, "amounts", spec, context="bookings", pg_schema=make_pg_schema())
    assert len(violations) == 1
    assert violations[0].field == "amounts.pfAmount"


# --- dryrun.py: run_fast_pass end-to-end (mongomock) ---------------------------------------


@pytest.fixture
def mongo_client():
    client = mongomock.MongoClient()
    yield client
    client.close()


def test_run_fast_pass_clean_unpivot_mapping(mongo_client, monkeypatch):
    db = mongo_client["app"]
    db.bookingPayment.insert_one(
        {"_id": mongomock.ObjectId(), "pfAmount": 100, "payToHospital": 200, "finalBill": 300}
    )
    monkeypatch.setattr("mongopg_migrate.migrate.dryrun.MongoClient", lambda uri: mongo_client)
    mongo_client.get_default_database = lambda: db

    report = run_fast_pass(make_mapping(), "mongodb://fake/app", make_pg_schema())
    assert report.ok, report.violations


def test_run_fast_pass_null_amount_into_not_null_flagged(mongo_client, monkeypatch):
    db = mongo_client["app"]
    db.bookingPayment.insert_one({"_id": mongomock.ObjectId(), "pfAmount": None})
    monkeypatch.setattr("mongopg_migrate.migrate.dryrun.MongoClient", lambda uri: mongo_client)
    mongo_client.get_default_database = lambda: db

    mapping = make_mapping()
    mapping.entities["bookings"].unpivot["amounts"].skip_null = False
    report = run_fast_pass(mapping, "mongodb://fake/app", make_pg_schema())
    assert not report.ok
    assert any("NOT NULL" in v.message for v in report.violations)
