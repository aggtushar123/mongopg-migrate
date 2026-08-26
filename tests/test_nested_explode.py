"""Coverage for nested `explode:` — a second embedded array one level down
(e.g. `hospitalDetails.facilities[].categoryParts[]` -> `HospitalFacility`
rows, each with its own `FacilityCategoryPart` child rows). Neither the
original flat `explode` nor `junction` could express two levels of
embedding; added in response to a real mapping shaped this way.

The middle level's own id has to be known *before* its row is COPYed (so it
can be threaded down as the grandchild's `parent_fk`) — that's why a level
with nested children can't use `serial` (schema.py rejects the combination)
and why `id_strategy` — previously a dead field for every `explode` level —
now does real work whenever nesting is present.
"""

import mongomock
import pytest

from mongopg_migrate.introspect.postgres import ColumnInfo, PostgresSchema, TableSchema
from mongopg_migrate.mapping.schema import (
    EntityMapping,
    ExplodeSpec,
    FieldSpec,
    ForeignKeyRef,
    IdStrategy,
    IdStrategyType,
    MappingFile,
)
from mongopg_migrate.migrate.dryrun import (
    _gather_explode_lookup_needs,
    _validate_explode_array_shapes,
    _validate_explode_item,
    run_fast_pass,
)
from mongopg_migrate.migrate.load import _collect_explode_rows, _flatten_explode


def col(name: str, data_type: str, nullable: bool = True) -> ColumnInfo:
    return ColumnInfo(name=name, data_type=data_type, is_nullable=nullable, default=None)


def make_nested_spec(*, category_field_extra: dict | None = None) -> ExplodeSpec:
    return ExplodeSpec(
        target="hospital_facilities",
        id_strategy=IdStrategy(type=IdStrategyType.OBJECTID_TO_UUID, source_field="facilityId"),
        parent_fk=ForeignKeyRef(target_field="hospital_id", references="hospitals.id"),
        fields={"name": FieldSpec(target="name")},
        explode={
            "categoryParts": ExplodeSpec(
                target="facility_category_parts",
                id_strategy=IdStrategy(type=IdStrategyType.SERIAL),
                parent_fk=ForeignKeyRef(target_field="facility_id", references="hospital_facilities.id"),
                fields={"partName": FieldSpec(target="part_name"), **(category_field_extra or {})},
            )
        },
    )


def make_pg_schema() -> PostgresSchema:
    return PostgresSchema(
        tables={
            "hospitals": TableSchema(name="hospitals", columns={"id": col("id", "uuid")}, primary_key=["id"]),
            "hospital_facilities": TableSchema(
                name="hospital_facilities",
                columns={
                    "id": col("id", "uuid"),
                    "hospital_id": col("hospital_id", "uuid", nullable=False),
                    "name": col("name", "text", nullable=False),
                },
                primary_key=["id"],
            ),
            "facility_category_parts": TableSchema(
                name="facility_category_parts",
                columns={
                    "id": col("id", "bigint"),
                    "facility_id": col("facility_id", "uuid", nullable=False),
                    "part_name": col("part_name", "text", nullable=False),
                },
                primary_key=["id"],
            ),
        }
    )


def make_mapping() -> MappingFile:
    return MappingFile(
        entities={
            "hospitals": EntityMapping(
                source="hospitals",
                target="hospitals",
                id_strategy=IdStrategy(type=IdStrategyType.OBJECTID_TO_UUID, source_field="_id"),
                explode={"facilities": make_nested_spec()},
            )
        }
    )


# --- schema.py: nesting + id_strategy validation --------------------------------------------


def test_serial_with_nested_children_is_rejected():
    with pytest.raises(ValueError, match="nested `explode` children but id_strategy"):
        ExplodeSpec(
            target="hospital_facilities",
            id_strategy=IdStrategy(type=IdStrategyType.SERIAL),
            parent_fk=ForeignKeyRef(target_field="hospital_id", references="hospitals.id"),
            explode={
                "categoryParts": ExplodeSpec(
                    target="facility_category_parts",
                    id_strategy=IdStrategy(type=IdStrategyType.SERIAL),
                    parent_fk=ForeignKeyRef(target_field="facility_id", references="hospital_facilities.id"),
                )
            },
        )


def test_leaf_level_may_still_use_serial():
    spec = make_nested_spec()
    assert spec.explode["categoryParts"].id_strategy.type == IdStrategyType.SERIAL


def test_middle_level_requires_source_field_for_non_serial_strategy():
    with pytest.raises(ValueError, match="source_field is required"):
        ExplodeSpec(
            target="hospital_facilities",
            id_strategy=IdStrategy(type=IdStrategyType.OBJECTID_TO_UUID),  # no source_field
            parent_fk=ForeignKeyRef(target_field="hospital_id", references="hospitals.id"),
            explode={
                "categoryParts": ExplodeSpec(
                    target="facility_category_parts",
                    id_strategy=IdStrategy(type=IdStrategyType.SERIAL),
                    parent_fk=ForeignKeyRef(target_field="facility_id", references="hospital_facilities.id"),
                )
            },
        )


# --- load.py: _flatten_explode ---------------------------------------------------------------


def test_flatten_explode_is_parent_before_child():
    flattened = _flatten_explode({"facilities": make_nested_spec()})
    paths = [p for p, _ in flattened]
    assert paths == ["facilities", "facilities.categoryParts"]


def test_flatten_explode_targets_match_specs():
    flattened = dict(_flatten_explode({"facilities": make_nested_spec()}))
    assert flattened["facilities"].target == "hospital_facilities"
    assert flattened["facilities.categoryParts"].target == "facility_category_parts"


# --- load.py: _collect_explode_rows -----------------------------------------------------------


def test_collect_explode_rows_threads_middle_level_id_to_grandchild():
    exp = make_nested_spec()
    pg_schema = make_pg_schema()
    explode_rows = {"facilities": [], "facilities.categoryParts": []}
    doc = {
        "facilityId": "F1",
        "name": "Cardiology Wing",
        "categoryParts": [{"partName": "ICU"}, {"partName": "OT"}],
    }
    _collect_explode_rows(
        "hospital-uuid-1",
        [doc],
        exp,
        path="facilities",
        context="hospitals",
        conn=None,
        pg_schema=pg_schema,
        id_buffers={},
        explode_rows=explode_rows,
        internal_schema="_mongopg",
        external_conns=None,
    )
    assert len(explode_rows["facilities"]) == 1
    facility_row = explode_rows["facilities"][0]
    # row = [parent_fk_value, own_id (since it has children), *fields]
    assert facility_row[0] == "hospital-uuid-1"
    facility_id = facility_row[1]
    assert facility_row[2] == "Cardiology Wing"

    assert len(explode_rows["facilities.categoryParts"]) == 2
    for grandchild_row in explode_rows["facilities.categoryParts"]:
        # leaf level: row = [parent_fk_value, *fields] — no own id (SERIAL, Postgres-assigned)
        assert grandchild_row[0] == facility_id
    assert {r[1] for r in explode_rows["facilities.categoryParts"]} == {"ICU", "OT"}


def test_collect_explode_rows_id_is_deterministic_across_calls():
    # objectid_to_uuid must be stable — required for resume idempotency.
    exp = make_nested_spec()
    pg_schema = make_pg_schema()
    doc = {"facilityId": "F1", "name": "Cardiology Wing", "categoryParts": []}

    rows_a = {"facilities": [], "facilities.categoryParts": []}
    _collect_explode_rows(
        "p1", [doc], exp, path="facilities", context="hospitals", conn=None, pg_schema=pg_schema,
        id_buffers={}, explode_rows=rows_a, internal_schema="_mongopg", external_conns=None,
    )
    rows_b = {"facilities": [], "facilities.categoryParts": []}
    _collect_explode_rows(
        "p1", [doc], exp, path="facilities", context="hospitals", conn=None, pg_schema=pg_schema,
        id_buffers={}, explode_rows=rows_b, internal_schema="_mongopg", external_conns=None,
    )
    assert rows_a["facilities"][0][1] == rows_b["facilities"][0][1]


def test_collect_explode_rows_empty_nested_array_produces_no_grandchild_rows():
    exp = make_nested_spec()
    pg_schema = make_pg_schema()
    doc = {"facilityId": "F1", "name": "Cardiology Wing"}  # categoryParts absent entirely
    explode_rows = {"facilities": [], "facilities.categoryParts": []}
    _collect_explode_rows(
        "p1", [doc], exp, path="facilities", context="hospitals", conn=None, pg_schema=pg_schema,
        id_buffers={}, explode_rows=explode_rows, internal_schema="_mongopg", external_conns=None,
    )
    assert len(explode_rows["facilities"]) == 1
    assert explode_rows["facilities.categoryParts"] == []


# --- dryrun.py: recursive validators ----------------------------------------------------------


def test_validate_explode_item_recurses_into_nested_not_null_violation():
    exp = make_nested_spec(category_field_extra=None)
    item = {"facilityId": "F1", "name": "Cardiology Wing", "categoryParts": [{"partName": None}]}
    pg_schema = make_pg_schema()
    violations = _validate_explode_item(item, exp, context="hospitals.facilities", pg_schema=pg_schema, found={})
    assert len(violations) == 1
    assert violations[0].field == "partName"
    assert "NOT NULL" in violations[0].message


def test_validate_explode_item_clean_when_all_present():
    exp = make_nested_spec()
    item = {"facilityId": "F1", "name": "Cardiology Wing", "categoryParts": [{"partName": "ICU"}]}
    pg_schema = make_pg_schema()
    violations = _validate_explode_item(item, exp, context="hospitals.facilities", pg_schema=pg_schema, found={})
    assert violations == []


def test_validate_explode_array_shapes_flags_scalar_at_nested_level():
    exp = make_nested_spec()
    item = {"facilityId": "F1", "name": "Cardiology Wing", "categoryParts": "not-a-list"}
    violations = _validate_explode_array_shapes(item, exp, entity_name="hospitals", field_path="facilities")
    assert len(violations) == 1
    assert violations[0].field == "facilities.categoryParts"
    assert "not an array" in violations[0].message


def test_gather_explode_lookup_needs_recurses_into_nested_lookup():
    exp = ExplodeSpec(
        target="hospital_facilities",
        id_strategy=IdStrategy(type=IdStrategyType.OBJECTID_TO_UUID, source_field="facilityId"),
        parent_fk=ForeignKeyRef(target_field="hospital_id", references="hospitals.id"),
        explode={
            "categoryParts": ExplodeSpec(
                target="facility_category_parts",
                id_strategy=IdStrategy(type=IdStrategyType.SERIAL),
                parent_fk=ForeignKeyRef(target_field="facility_id", references="hospital_facilities.id"),
                fields={"deptId": FieldSpec(target="department_id", lookup="departments")},
            )
        },
    )
    item = {"facilityId": "F1", "categoryParts": [{"deptId": "D1"}, {"deptId": "D2"}]}
    needs = _gather_explode_lookup_needs(item, exp)
    assert needs == {"departments": {"D1", "D2"}}


# --- dryrun.py: run_fast_pass end-to-end (mongomock) -------------------------------------------


@pytest.fixture
def mongo_client():
    client = mongomock.MongoClient()
    yield client
    client.close()


def test_run_fast_pass_clean_nested_mapping(mongo_client, monkeypatch):
    db = mongo_client["app"]
    db.hospitals.insert_one(
        {
            "_id": mongomock.ObjectId(),
            "facilities": [
                {"facilityId": "F1", "name": "Cardiology Wing", "categoryParts": [{"partName": "ICU"}]},
            ],
        }
    )
    monkeypatch.setattr("mongopg_migrate.migrate.dryrun.MongoClient", lambda uri: mongo_client)
    mongo_client.get_default_database = lambda: db

    report = run_fast_pass(make_mapping(), "mongodb://fake/app", make_pg_schema())
    assert report.ok, report.violations


def test_run_fast_pass_flags_nested_not_null(mongo_client, monkeypatch):
    db = mongo_client["app"]
    db.hospitals.insert_one(
        {
            "_id": mongomock.ObjectId(),
            "facilities": [
                {"facilityId": "F1", "name": "Cardiology Wing", "categoryParts": [{"partName": None}]},
            ],
        }
    )
    monkeypatch.setattr("mongopg_migrate.migrate.dryrun.MongoClient", lambda uri: mongo_client)
    mongo_client.get_default_database = lambda: db

    report = run_fast_pass(make_mapping(), "mongodb://fake/app", make_pg_schema())
    assert not report.ok
    assert any("NOT NULL" in v.message and v.field == "partName" for v in report.violations)


def test_run_fast_pass_flags_nested_scalar_where_array_expected(mongo_client, monkeypatch):
    db = mongo_client["app"]
    db.hospitals.insert_one(
        {
            "_id": mongomock.ObjectId(),
            "facilities": [
                {"facilityId": "F1", "name": "Cardiology Wing", "categoryParts": "ICU"},
            ],
        }
    )
    monkeypatch.setattr("mongopg_migrate.migrate.dryrun.MongoClient", lambda uri: mongo_client)
    mongo_client.get_default_database = lambda: db

    report = run_fast_pass(make_mapping(), "mongodb://fake/app", make_pg_schema())
    assert not report.ok
    assert any("facilities.categoryParts" == v.field for v in report.violations)
