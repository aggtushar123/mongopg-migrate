"""SIGKILL mid-run, then resume — the specific claim this integration
suite exists to keep CI honest about (the README has claimed this, live-
tested by hand, since early in the project's history; this is what makes
it a fact CI checks on every push instead of prose someone has to trust).

Real subprocess, real SIGKILL — this can't be done through CliRunner
in-process (killing that would kill the pytest process running it), so
this one test in the suite shells out to `python -m mongopg_migrate.cli`
exactly as a real user would invoke the installed console script.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

from bson import ObjectId

# Calibrated against local Docker: 5000 docs at batch-size 10 (500 batches,
# each its own Mongo find + Postgres COPY + per-doc id_map INSERT) takes
# ~2s end to end — enough wall-clock time that polling for a partial,
# non-empty, non-final row count reliably lands mid-run rather than racing
# a fixed sleep against however fast this particular machine happens to be.
N_DOCS = 5000
BATCH_SIZE = 10


def _seed(mongo_db):
    mongo_db.resume_test_items.insert_many(
        [{"_id": ObjectId(f"{i:024x}"), "value": i} for i in range(N_DOCS)]
    )


def _mapping_yaml() -> str:
    return """
    entities:
      resume_test_items:
        source: resume_test_items
        target: resume_test_items
        id_strategy: {type: objectid_to_uuid, source_field: _id}
        fields:
          value: value
    """


def _run_migrate(mapping_path, mongo_uri: str, postgres_uri: str) -> subprocess.Popen:
    env = {**os.environ, "MONGO_URI": mongo_uri, "POSTGRES_URI": postgres_uri}
    return subprocess.Popen(
        [
            sys.executable, "-m", "mongopg_migrate.cli", "migrate", str(mapping_path),
            "--mode", "append", "--batch-size", str(BATCH_SIZE),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_sigkill_mid_migrate_then_resume_loses_nothing_and_duplicates_nothing(
    mongo_db, pg_conn, mongo_uri, postgres_uri, tmp_path
):
    _seed(mongo_db)
    with pg_conn.cursor() as cur:
        cur.execute("CREATE TABLE resume_test_items (id UUID PRIMARY KEY, value INT NOT NULL)")

    mapping_path = tmp_path / "mapping.yaml"
    mapping_path.write_text(_mapping_yaml())

    try:
        proc = _run_migrate(mapping_path, mongo_uri, postgres_uri)
        # Poll for real, visible partial progress instead of guessing a
        # fixed sleep against however fast this particular machine happens
        # to be — kill the instant a batch has landed but the whole load
        # hasn't, which is the actual condition this test needs, not a
        # timing proxy for it.
        deadline = time.time() + 15
        partial_count = 0
        while time.time() < deadline:
            if proc.poll() is not None:
                break  # exited before any partial state was ever observed
            with pg_conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM resume_test_items")
                (partial_count,) = cur.fetchone()
            if 0 < partial_count < N_DOCS:
                break
            time.sleep(0.02)

        still_running = proc.poll() is None
        proc.kill()
        proc.wait(timeout=10)

        assert still_running, (
            "migrate finished before the kill landed — this test proves nothing about resume "
            "unless the process was still working when killed; widen N_DOCS or shrink BATCH_SIZE"
        )
        assert 0 < partial_count < N_DOCS, (
            f"expected a partial load strictly between 0 and {N_DOCS} at kill time, got {partial_count} "
            "— either nothing landed before the kill, or the kill landed after completion"
        )

        # Resume: same command, same mode. Must pick up exactly where the
        # killed run left off — no re-inserted duplicates of already-
        # committed rows, no gap left behind either.
        resume = subprocess.run(
            [
                sys.executable, "-m", "mongopg_migrate.cli", "migrate", str(mapping_path),
                "--mode", "append", "--batch-size", str(BATCH_SIZE),
            ],
            env={**os.environ, "MONGO_URI": mongo_uri, "POSTGRES_URI": postgres_uri},
            capture_output=True, text=True, timeout=60, check=False,
        )
        assert resume.returncode == 0, resume.stderr

        with pg_conn.cursor() as cur:
            cur.execute("SELECT count(*), count(DISTINCT id), count(DISTINCT value) FROM resume_test_items")
            total, distinct_ids, distinct_values = cur.fetchone()
        assert total == N_DOCS, f"expected all {N_DOCS} documents present after resume, got {total}"
        assert distinct_ids == N_DOCS, "duplicate ids after resume — the same document was loaded twice"
        assert distinct_values == N_DOCS, "duplicate values after resume — the same document was loaded twice"
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS resume_test_items")
            cur.execute("DELETE FROM _mongopg.load_checkpoint WHERE entity = 'resume_test_items'")
            cur.execute("DELETE FROM _mongopg.id_map WHERE entity = 'resume_test_items'")
        mongo_db.resume_test_items.drop()
