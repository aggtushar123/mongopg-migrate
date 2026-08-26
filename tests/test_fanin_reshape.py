"""Tests for scripts/fanin_reshape.py — the standalone Stage-0 helper for
the fan-in case (N Mongo documents -> 1 derived document) the core tool's
mapping DSL deliberately does not support (PRD §4 non-goal). Deliberately
outside src/mongopg_migrate; imported here by file path since it isn't an
installed package module.

$merge isn't implemented by mongomock (confirmed directly: it raises
NotImplementedError), so the $merge write path itself is live-Docker-
verified rather than unit-tested here — same split this project already
uses elsewhere (e.g. test_load_upsert.py tests SQL generation without a
live DB; the real upsert behavior is verified live). Everything else —
pipeline construction, validation/error paths, and the full $out write
path — is covered here with mongomock or no DB at all.
"""

import importlib.util
import json
import sys
from pathlib import Path

import mongomock
import pytest

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "fanin_reshape.py"
_spec = importlib.util.spec_from_file_location("fanin_reshape", SCRIPT_PATH)
fanin_reshape = importlib.util.module_from_spec(_spec)
sys.modules["fanin_reshape"] = fanin_reshape
_spec.loader.exec_module(fanin_reshape)

build_pickone_pipeline = fanin_reshape.build_pickone_pipeline
build_output_stage = fanin_reshape.build_output_stage
load_custom_pipeline = fanin_reshape.load_custom_pipeline
run_reshape = fanin_reshape.run_reshape
ReshapeError = fanin_reshape.ReshapeError


# --- build_pickone_pipeline --------------------------------------------------------------


def test_pickone_pipeline_single_group_field():
    pipeline = build_pickone_pipeline(["bookingId"], "updatedAt")
    assert pipeline == [
        {"$sort": {"updatedAt": -1}},
        {"$group": {"_id": "$bookingId", "doc": {"$first": "$$ROOT"}}},
        {"$replaceRoot": {"newRoot": "$doc"}},
    ]


def test_pickone_pipeline_multi_group_field_uses_subdocument_id():
    pipeline = build_pickone_pipeline(["bookingId", "region"], "updatedAt")
    group_stage = pipeline[1]["$group"]
    assert group_stage["_id"] == {"bookingId": "$bookingId", "region": "$region"}


def test_pickone_pipeline_asc_order_flips_sort_direction():
    pipeline = build_pickone_pipeline(["bookingId"], "updatedAt", pick_order="asc")
    assert pipeline[0]["$sort"]["updatedAt"] == 1


def test_pickone_pipeline_prepends_match_when_filter_given():
    pipeline = build_pickone_pipeline(["bookingId"], "updatedAt", match_filter={"env": "prod"})
    assert pipeline[0] == {"$match": {"env": "prod"}}
    assert pipeline[1]["$sort"] == {"updatedAt": -1}


def test_pickone_pipeline_no_match_stage_when_no_filter():
    pipeline = build_pickone_pipeline(["bookingId"], "updatedAt")
    assert not any("$match" in stage for stage in pipeline)


def test_pickone_pipeline_rejects_bad_pick_order():
    with pytest.raises(ReshapeError, match="pick_order"):
        build_pickone_pipeline(["bookingId"], "updatedAt", pick_order="sideways")


# --- build_output_stage --------------------------------------------------------------------


def test_output_stage_out():
    assert build_output_stage("out", "dest_coll", None) == {"$out": "dest_coll"}


def test_output_stage_merge_single_key():
    stage = build_output_stage("merge", "dest_coll", ["bookingId"])
    assert stage == {
        "$merge": {
            "into": "dest_coll",
            "on": "bookingId",
            "whenMatched": "replace",
            "whenNotMatched": "insert",
        }
    }


def test_output_stage_merge_composite_key():
    stage = build_output_stage("merge", "dest_coll", ["bookingId", "region"])
    assert stage["$merge"]["on"] == ["bookingId", "region"]


def test_output_stage_merge_without_key_raises():
    with pytest.raises(ReshapeError, match="merge_on"):
        build_output_stage("merge", "dest_coll", None)


def test_output_stage_bad_mode_raises():
    with pytest.raises(ReshapeError, match="mode must be"):
        build_output_stage("replace-everything", "dest_coll", None)


# --- load_custom_pipeline --------------------------------------------------------------------


def test_load_custom_pipeline_reads_json_array(tmp_path):
    p = tmp_path / "pipeline.json"
    p.write_text(json.dumps([{"$match": {"status": "APPROVED"}}]))
    assert load_custom_pipeline(str(p)) == [{"$match": {"status": "APPROVED"}}]


def test_load_custom_pipeline_rejects_non_array(tmp_path):
    p = tmp_path / "pipeline.json"
    p.write_text(json.dumps({"$match": {"status": "APPROVED"}}))
    with pytest.raises(ReshapeError, match="expected a JSON array"):
        load_custom_pipeline(str(p))


def test_load_custom_pipeline_rejects_embedded_out_stage(tmp_path):
    p = tmp_path / "pipeline.json"
    p.write_text(json.dumps([{"$sort": {"updatedAt": -1}}, {"$out": "sneaky"}]))
    with pytest.raises(ReshapeError, match=r"\$out/\$merge"):
        load_custom_pipeline(str(p))


def test_load_custom_pipeline_rejects_embedded_merge_stage(tmp_path):
    p = tmp_path / "pipeline.json"
    p.write_text(json.dumps([{"$merge": {"into": "sneaky"}}]))
    with pytest.raises(ReshapeError, match=r"\$out/\$merge"):
        load_custom_pipeline(str(p))


# --- run_reshape: refuses source == dest ---------------------------------------------------


def test_run_reshape_refuses_source_equals_dest():
    with pytest.raises(ReshapeError, match="itself"):
        run_reshape(
            "mongodb://fake/app", "app", "same", "same", [], mode="out", merge_on=None, dry_run=True
        )


# --- run_reshape: end-to-end against mongomock (--mode out path) --------------------------


@pytest.fixture
def mongo_client(monkeypatch):
    client = mongomock.MongoClient()
    monkeypatch.setattr(fanin_reshape, "MongoClient", lambda uri: client)
    yield client
    client.close()


def _seed_kyc_steps(db):
    db.kycSteps.insert_many(
        [
            {"bookingId": "B1", "status": "PENDING", "updatedAt": 1},
            {"bookingId": "B1", "status": "IN_REVIEW", "updatedAt": 2},
            {"bookingId": "B1", "status": "APPROVED", "updatedAt": 3},
            {"bookingId": "B2", "status": "PENDING", "updatedAt": 1},
        ]
    )


def test_run_reshape_out_picks_latest_per_group(mongo_client):
    db = mongo_client["app"]
    _seed_kyc_steps(db)
    pipeline = build_pickone_pipeline(["bookingId"], "updatedAt")

    result = run_reshape(
        "mongodb://fake/app", "app", "kycSteps", "kycSteps_latest", pipeline,
        mode="out", merge_on=None, dry_run=False,
    )

    assert result["source_count"] == 4
    assert result["dest_count"] == 2
    docs_by_booking = {d["bookingId"]: d for d in db.kycSteps_latest.find({}, {"_id": 0})}
    assert docs_by_booking["B1"]["status"] == "APPROVED"  # highest updatedAt wins
    assert docs_by_booking["B2"]["status"] == "PENDING"


def test_run_reshape_out_picks_earliest_when_asc(mongo_client):
    db = mongo_client["app"]
    _seed_kyc_steps(db)
    pipeline = build_pickone_pipeline(["bookingId"], "updatedAt", pick_order="asc")

    run_reshape(
        "mongodb://fake/app", "app", "kycSteps", "kycSteps_earliest", pipeline,
        mode="out", merge_on=None, dry_run=False,
    )

    docs_by_booking = {d["bookingId"]: d for d in db.kycSteps_earliest.find({}, {"_id": 0})}
    assert docs_by_booking["B1"]["status"] == "PENDING"  # lowest updatedAt wins


def test_run_reshape_out_replaces_dest_on_rerun(mongo_client):
    # $out semantics: the whole dest collection is replaced each run, not
    # appended to — confirm a second run with fewer source docs shrinks dest.
    db = mongo_client["app"]
    _seed_kyc_steps(db)
    pipeline = build_pickone_pipeline(["bookingId"], "updatedAt")
    run_reshape(
        "mongodb://fake/app", "app", "kycSteps", "kycSteps_latest", pipeline,
        mode="out", merge_on=None, dry_run=False,
    )
    assert db.kycSteps_latest.count_documents({}) == 2

    db.kycSteps.delete_many({"bookingId": "B2"})
    run_reshape(
        "mongodb://fake/app", "app", "kycSteps", "kycSteps_latest", pipeline,
        mode="out", merge_on=None, dry_run=False,
    )
    assert db.kycSteps_latest.count_documents({}) == 1


def test_run_reshape_dry_run_writes_nothing(mongo_client):
    db = mongo_client["app"]
    _seed_kyc_steps(db)
    pipeline = build_pickone_pipeline(["bookingId"], "updatedAt")

    result = run_reshape(
        "mongodb://fake/app", "app", "kycSteps", "kycSteps_latest", pipeline,
        mode="out", merge_on=None, dry_run=True, preview_limit=1,
    )

    assert result["dry_run"] is True
    assert result["source_count"] == 4
    assert result["would_write_count"] == 2
    assert len(result["preview"]) == 1
    assert "kycSteps_latest" not in db.list_collection_names()


def test_run_reshape_replace_root_gives_flat_original_shape(mongo_client):
    # The whole point of $replaceRoot: output docs are NOT wrapped under
    # {_id: <group key>, doc: {...}} — they look exactly like the original
    # documents, which is what makes --dest mappable by the core tool.
    db = mongo_client["app"]
    _seed_kyc_steps(db)
    pipeline = build_pickone_pipeline(["bookingId"], "updatedAt")
    run_reshape(
        "mongodb://fake/app", "app", "kycSteps", "kycSteps_latest", pipeline,
        mode="out", merge_on=None, dry_run=False,
    )
    doc = db.kycSteps_latest.find_one({"bookingId": "B1"})
    assert set(doc.keys()) >= {"_id", "bookingId", "status", "updatedAt"}
    assert "doc" not in doc
