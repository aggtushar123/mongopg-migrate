"""Turn a failed database connection into a message instead of a traceback.

The most likely first error for anyone who has just installed this is a URI
that points at nothing — a typo'd port, a container that is not up, a VPN
that is not connected. Until now that surfaced as a raw
`pymongo.errors.ServerSelectionTimeoutError` carrying a full topology dump,
or a `psycopg.OperationalError` reporting the same refusal twice (once for
IPv6, once for IPv4). Both are accurate and neither tells a new user what to
do.

Every other failure in this CLI is reported as an actionable message; this
path only escaped because the authors always had their databases running.

The URI is echoed back so the user can see what was actually used — which is
half the diagnosis when `MONGO_URI` is set in the environment and a `--flag`
was expected to win — but any password in it is masked first. A tool that
prints credentials into a terminal, a CI log or a pasted bug report is a tool
that leaks them.
"""

from __future__ import annotations

import functools
import re
import sys

import click
import psycopg
import pymongo.errors

__all__ = ["friendly_connection_errors", "redact_uri"]

# scheme://user:PASSWORD@host — the URL form used by both mongodb:// and
# postgresql:// connection strings.
_URL_PASSWORD = re.compile(r"(?P<prefix>://[^:/?#@]+:)(?P<secret>[^@/?#]*)(?P<suffix>@)")
# libpq keyword form: "host=... password=SECRET sslmode=..."
_KEYWORD_PASSWORD = re.compile(r"(?P<prefix>\bpassword\s*=\s*)(?P<secret>'[^']*'|\S+)")


def redact_uri(uri: str | None) -> str:
    """Mask the password in a connection string, in either accepted form."""
    if not uri:
        return "(not set)"
    redacted = _URL_PASSWORD.sub(lambda m: f"{m.group('prefix')}***{m.group('suffix')}", uri)
    return _KEYWORD_PASSWORD.sub(lambda m: f"{m.group('prefix')}***", redacted)


def _first_line(exc: Exception) -> str:
    """The useful sentence, without the topology dump.

    pymongo appends the full `TopologyDescription` to its message and psycopg
    repeats the same refusal once per resolved address. Neither adds anything
    for the case this module exists to explain.
    """
    text = str(exc).strip()
    for cut in ("(configured timeouts:", ", Timeout:", "Multiple connection attempts failed"):
        idx = text.find(cut)
        if idx > 0:
            text = text[:idx]
    return text.strip().rstrip(",").strip() or exc.__class__.__name__


def _report(kind: str, flag: str, env: str, uri: str | None, exc: Exception, hints: list[str]) -> None:
    click.echo(f"ERROR: could not connect to {kind}.", err=True)
    click.echo(f"  {flag}: {redact_uri(uri)}", err=True)
    click.echo(f"  reason: {_first_line(exc)}", err=True)
    click.echo("", err=True)
    for hint in hints:
        click.echo(f"  - {hint}", err=True)
    click.echo(f"\n  (the value above came from {flag} or ${env})", err=True)
    sys.exit(1)


_MONGO_HINTS = [
    "Is the server running and reachable from here? A container may need to be started, or a VPN connected.",
    "Check the host and port. Inside a container the host is usually the service name, not `localhost`.",
    "The URI must include a database name, e.g. mongodb://localhost:27017/mydb.",
]
_POSTGRES_HINTS = [
    "Is the server running and reachable from here? A container may need to be started, or a VPN connected.",
    "Check the port. A Docker-published port on the host is often not 5432 — inside the compose network it is.",
    "Both URL (postgresql://user:pass@host:port/db) and libpq keyword form are accepted.",
]


def friendly_connection_errors(fn):
    """Report a failed connection as a message, wherever in the call it happens.

    Wrapping the whole command rather than the individual connect calls is
    deliberate: `migrate` and `validate` open their connections several layers
    down, and `dry-run` opens one per layer. Catching at the boundary covers
    all of them, including any added later.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (pymongo.errors.ServerSelectionTimeoutError, pymongo.errors.ConnectionFailure) as e:
            _report("MongoDB", "--mongo-uri", "MONGO_URI", kwargs.get("mongo_uri"), e, _MONGO_HINTS)
        except (pymongo.errors.InvalidURI, pymongo.errors.ConfigurationError) as e:
            _report("MongoDB", "--mongo-uri", "MONGO_URI", kwargs.get("mongo_uri"), e, _MONGO_HINTS)
        except psycopg.OperationalError as e:
            _report(
                "PostgreSQL", "--postgres-uri", "POSTGRES_URI", kwargs.get("postgres_uri"), e, _POSTGRES_HINTS
            )

    return wrapper
