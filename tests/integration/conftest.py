"""Fixtures shared by the real-Mongo/real-Postgres integration tests in
this directory.

Deliberately separate from the main `tests/` tree (which is pure unit
tests, no network, runs in ~4s): these tests are the CI answer to a real
review comment — "SIGKILL-resume, Docker-network runs, and a real 401
probe are claimed in the README... consider capturing them as a
compose-based integration test so CI proves them, not prose." They only
run when MONGO_URI/POSTGRES_URI are set (matching the CLI's own env var
convention) — never by accident against a developer's real local Mongo/
Postgres, and skipped cleanly (not failed) when unset, so `pytest -q` from
the repo root stays exactly as fast and offline as it always was.

Run locally the same way CI does:
    docker compose up -d
    MONGO_URI=mongodb://localhost:27017/app \
    POSTGRES_URI=postgresql://postgres:postgres@localhost:55432/app \
    pytest -q tests/integration
"""

from __future__ import annotations

import os

import psycopg
import pytest
from pymongo import MongoClient

MONGO_URI = os.environ.get("MONGO_URI")
POSTGRES_URI = os.environ.get("POSTGRES_URI")

_SKIP_REASON = "integration tests need real MONGO_URI/POSTGRES_URI env vars — see this file's module docstring"


def pytest_collection_modifyitems(items):
    # A module-level `pytestmark` in conftest.py does NOT propagate to
    # sibling test files — it only marks tests defined in conftest.py
    # itself (none here). This hook is what actually applies the skip to
    # every test collected under tests/integration/, which is what the
    # module docstring's "skipped cleanly, not failed" promise depends on.
    if MONGO_URI and POSTGRES_URI:
        return
    skip = pytest.mark.skip(reason=_SKIP_REASON)
    for item in items:
        if "tests/integration/" in str(item.fspath).replace(os.sep, "/"):
            item.add_marker(skip)


@pytest.fixture
def mongo_uri() -> str:
    return MONGO_URI


@pytest.fixture
def postgres_uri() -> str:
    return POSTGRES_URI


@pytest.fixture
def mongo_client():
    client = MongoClient(MONGO_URI)
    yield client
    client.close()


@pytest.fixture
def mongo_db(mongo_client):
    return mongo_client.get_default_database()


@pytest.fixture
def pg_conn():
    with psycopg.connect(POSTGRES_URI, autocommit=True) as conn:
        yield conn
