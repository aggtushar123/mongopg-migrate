"""Connection failures are reported, not raised.

A URI pointing at nothing is the most likely first error for anyone who has
just installed this, and it was the one path in the CLI that answered with a
stack trace. The redaction half matters more than it looks: this prints the
URI back so the user can see what was actually used, and connection strings
routinely carry a password.
"""

from __future__ import annotations

import click
import psycopg
import pymongo.errors
import pytest
from click.testing import CliRunner

from mongopg_migrate.connerrors import friendly_connection_errors, redact_uri

# ── redaction ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("postgresql://user:hunter2@host:5432/db", "postgresql://user:***@host:5432/db"),
        ("mongodb://admin:s3cret@mongo:27017/app", "mongodb://admin:***@mongo:27017/app"),
        ("mongodb+srv://u:p@cluster.example.net/app", "mongodb+srv://u:***@cluster.example.net/app"),
        ("host=db port=5432 password=hunter2 user=u", "host=db port=5432 password=*** user=u"),
        ("host=db password='has spaces' user=u", "host=db password=*** user=u"),
    ],
)
def test_passwords_are_masked_in_both_accepted_forms(uri, expected):
    assert redact_uri(uri) == expected


@pytest.mark.parametrize(
    "uri",
    [
        "postgresql://host:5432/db",          # no credentials at all
        "mongodb://localhost:27017/app",
        "host=localhost port=5432 dbname=app",
    ],
)
def test_a_uri_without_a_password_is_unchanged(uri):
    assert redact_uri(uri) == uri


def test_a_missing_uri_is_described_rather_than_crashing():
    assert redact_uri(None) == "(not set)"


def test_the_secret_never_appears_in_the_output():
    # The property that actually matters, stated directly.
    assert "hunter2" not in redact_uri("postgresql://u:hunter2@h/db")
    assert "hunter2" not in redact_uri("host=h password=hunter2")


# ── the decorator ────────────────────────────────────────────────────────────


def _command(exc):
    @click.command()
    @click.option("--mongo-uri")
    @click.option("--postgres-uri")
    @friendly_connection_errors
    def cmd(mongo_uri, postgres_uri):
        raise exc

    return cmd


MONGO_DOWN = pymongo.errors.ServerSelectionTimeoutError(
    "localhost:29999: [Errno 61] Connection refused (configured timeouts: socketTimeoutMS: 20000.0ms), "
    "Timeout: 30s, Topology Description: <TopologyDescription id: abc, servers: [...]>"
)
PG_DOWN = psycopg.OperationalError(
    'connection failed: connection to server at "127.0.0.1", port 59999 failed: Connection refused\n'
    "Multiple connection attempts failed. All failures were:\n- host: 'localhost' ..."
)


def _run(exc, **params):
    args = []
    for k, v in params.items():
        args += [f"--{k.replace('_', '-')}", v]
    return CliRunner().invoke(_command(exc), args)


def test_a_mongo_connection_failure_exits_non_zero_without_a_traceback():
    result = _run(MONGO_DOWN, mongo_uri="mongodb://localhost:29999/app")
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "could not connect to MongoDB" in result.output


def test_the_mongo_message_names_the_uri_and_the_reason():
    result = _run(MONGO_DOWN, mongo_uri="mongodb://localhost:29999/app")
    assert "mongodb://localhost:29999/app" in result.output
    assert "Connection refused" in result.output


def test_the_topology_dump_is_not_shown():
    # Accurate, enormous, and no help to someone who mistyped a port.
    result = _run(MONGO_DOWN, mongo_uri="mongodb://localhost:29999/app")
    assert "TopologyDescription" not in result.output
    assert "socketTimeoutMS" not in result.output


def test_a_postgres_connection_failure_is_reported_the_same_way():
    result = _run(PG_DOWN, postgres_uri="postgresql://postgres:postgres@localhost:59999/app")
    assert result.exit_code == 1
    assert "could not connect to PostgreSQL" in result.output
    assert "Traceback" not in result.output


def test_the_duplicated_address_list_is_not_shown():
    result = _run(PG_DOWN, postgres_uri="postgresql://postgres:postgres@localhost:59999/app")
    assert "Multiple connection attempts failed" not in result.output


def test_a_password_in_the_uri_is_not_echoed_to_the_terminal():
    result = _run(PG_DOWN, postgres_uri="postgresql://postgres:hunter2@localhost:59999/app")
    assert "hunter2" not in result.output
    assert "postgresql://postgres:***@localhost:59999/app" in result.output


def test_the_message_says_where_the_value_came_from():
    # Half the diagnosis when MONGO_URI is set in the environment and a flag
    # was expected to win.
    result = _run(MONGO_DOWN, mongo_uri="mongodb://localhost:29999/app")
    assert "$MONGO_URI" in result.output


def test_an_unrelated_error_is_not_swallowed():
    # The decorator must not turn every failure into a connection message.
    result = _run(ValueError("something else entirely"))
    assert isinstance(result.exception, ValueError)


def test_a_successful_command_is_unaffected():
    @click.command()
    @click.option("--mongo-uri")
    @friendly_connection_errors
    def ok(mongo_uri):
        click.echo("done")

    result = CliRunner().invoke(ok, ["--mongo-uri", "mongodb://x/y"])
    assert result.exit_code == 0
    assert "done" in result.output
