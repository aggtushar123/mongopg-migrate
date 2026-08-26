"""Resolve a new target id for a source document per its entity's
`id_strategy` (PRD §12).

Every strategy except `serial` returns both the Python value to put in the
target column (already adapted to a type psycopg can write directly:
`uuid.UUID`, `int`, or `str`) and its canonical string form for storage in
`_mongopg.id_map` (PRD §7) — id_map columns are TEXT, so lookups always
compare on the string form regardless of the target column's real type.

`objectid_to_uuid` is deterministic (UUIDv5 of the source id under a fixed
namespace), which is what makes it safe to resume: re-deriving the same
source document's id after a kill mid-run reproduces the exact same target
id rather than allocating a new one.

`int_sequence` reserves ids in blocks (`SELECT nextval(seq) FROM
generate_series(1, N)`, one query returning N values) rather than one
`nextval()` round-trip per document — callers opt in by passing a mutable
`id_buffer` dict they own and keep alive across a whole entity's load (see
migrate/load.py), not just one batch. A block partially consumed when a
run is killed leaves a gap in the sequence, same as any rolled-back
`INSERT` into a real `SERIAL` column would — Postgres explicitly documents
sequence gaps as normal and harmless, so this doesn't trade away anything
resume already didn't already imply.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

import psycopg

from mongopg_migrate.mapping.schema import IdStrategy, IdStrategyType

# Fixed, arbitrary namespace so objectid_to_uuid is deterministic across
# runs/processes/machines. Derived once via:
#   uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/mongopg-migrate")
# and hardcoded — never recompute this at runtime, or resumed loads would
# silently generate different UUIDs for the same source document.
NAMESPACE = uuid.UUID("1f698f50-5e51-5fee-8985-c4ed74dd9d22")

_SEQUENCE_DEFAULT_RE = re.compile(r"nextval\('([^']+)'")
DEFAULT_RESERVE_BLOCK_SIZE = 500


class IdStrategyError(Exception):
    pass


def _reserve_block(conn: psycopg.Connection, seq_name: str, block_size: int) -> list[int]:
    with conn.cursor() as cur:
        cur.execute("SELECT nextval(%s) FROM generate_series(1, %s)", (seq_name, block_size))
        values = [row[0] for row in cur.fetchall()]
    values.reverse()  # pop() from the end below == dispense in the order fetched, O(1) per pop
    return values


@dataclass
class ResolvedId:
    column_value: object  # what to write into the target column
    str_form: str  # canonical string form, stored in _mongopg.id_map


def resolve_new_id(
    strategy: IdStrategy,
    source_id: object,
    *,
    conn: psycopg.Connection | None = None,
    column_default: str | None = None,
    id_buffer: dict[str, list[int]] | None = None,
    reserve_block_size: int = DEFAULT_RESERVE_BLOCK_SIZE,
) -> ResolvedId:
    if strategy.type == IdStrategyType.OBJECTID_TO_UUID:
        target = uuid.uuid5(NAMESPACE, str(source_id))
        return ResolvedId(column_value=target, str_form=str(target))

    if strategy.type == IdStrategyType.PASSTHROUGH:
        s = str(source_id)
        return ResolvedId(column_value=s, str_form=s)

    if strategy.type == IdStrategyType.UUID_GENERATE:
        target = uuid.uuid4()
        return ResolvedId(column_value=target, str_form=str(target))

    if strategy.type == IdStrategyType.INT_SEQUENCE:
        if conn is None or not column_default:
            raise IdStrategyError(
                "int_sequence id_strategy requires a live connection and the target "
                "column's default (to find its sequence) — got "
                f"conn={conn!r} column_default={column_default!r}"
            )
        m = _SEQUENCE_DEFAULT_RE.search(column_default)
        if not m:
            raise IdStrategyError(
                f"int_sequence id_strategy: target column default {column_default!r} "
                "doesn't look like nextval('...') — is it really a serial/identity column?"
            )
        seq_name = m.group(1)
        if id_buffer is None:
            with conn.cursor() as cur:
                cur.execute("SELECT nextval(%s)", (seq_name,))
                (value,) = cur.fetchone()
        else:
            buf = id_buffer.setdefault(seq_name, [])
            if not buf:
                buf.extend(_reserve_block(conn, seq_name, reserve_block_size))
            value = buf.pop()
        return ResolvedId(column_value=value, str_form=str(value))

    raise IdStrategyError(
        f"resolve_new_id() doesn't apply to id_strategy.type={strategy.type.value!r} "
        "(SERIAL child rows get their id from Postgres itself at COPY time, not from here)"
    )
