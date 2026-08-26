"""The small transform DSL referenced by `FieldSpec.transform` (PRD §7:
"Small transform DSL in the mapping file: cast, default, split,
json_extract, enum mapping") — all five now implemented (`enum:` and
`split:` were the last two; both came out of a real cross-DB migration
where an ORM's stored enum labels didn't match the target Postgres
column's).

`json_extract:<path>` is not handled here — `mapping/propose.py` already
encodes the nested path directly into the field's dict key (e.g.
`"shippingAddress.city"`), and `get_nested()` in this module resolves that
key against the source document regardless of the `transform` string. The
`json_extract:` prefix is informational/self-documenting in the mapping
file, not something this module dispatches on.

Everything here is deliberately small: PRD §10 acknowledges "transform DSL
signatures beyond cast_timestamptz" are fine to decide in code rather than
pin down in the PRD. This registry is meant to grow field-by-field as real
mappings need more of it — it is not trying to be a general expression
language.
"""

from __future__ import annotations

import datetime
import decimal
import json
from typing import Any


class TransformError(Exception):
    pass


def get_nested(doc: dict, dotted_path: str) -> Any:
    """Resolve a dotted field path (as produced by mapping/propose.py for
    flattened nested objects, e.g. `shippingAddress.city`) against a Mongo
    document. Returns None if any segment is missing."""
    value: Any = doc
    for segment in dotted_path.split("."):
        if not isinstance(value, dict) or segment not in value:
            return None
        value = value[segment]
    return value


def apply_transform(transform: str | None, value: Any) -> Any:
    if transform is None or value is None:
        return value
    if transform.startswith("json_extract:"):
        return value  # already resolved via the field's dotted key — see module docstring
    if transform.startswith("default:"):
        return value  # default only applies when value is None, handled by caller
    if transform == "cast_timestamptz":
        return _cast_timestamptz(value)
    if transform == "cast_int":
        return _cast(int, value, transform)
    if transform == "cast_float":
        return _cast(float, value, transform)
    if transform == "cast_text":
        return _cast(str, value, transform)
    if transform == "cast_bool":
        return _cast(bool, value, transform)
    if transform.startswith("enum:"):
        return _apply_enum(transform, value)
    if transform.startswith("split:"):
        return _apply_split(transform, value)
    raise TransformError(f"unrecognized transform {transform!r} — see migrate/transform.py")


def _cast(pytype: type, value: Any, transform: str) -> Any:
    try:
        return pytype(value)
    except (ValueError, TypeError) as e:
        raise TransformError(f"{transform}: cannot cast {value!r} ({type(value).__name__}): {e}") from e


def _apply_enum(transform: str, value: Any) -> Any:
    """`enum:<json object>` remaps a raw value through an explicit lookup
    table — e.g. `enum:{"1": "active", "2": "inactive"}` for a source
    system's integer/short-code enum landing on a Postgres column with
    different labels (the most common real gap: a Prisma/ORM enum whose
    stored values don't match the target column's labels verbatim). A `"*"`
    key is an explicit fallback for anything not otherwise listed; without
    one, an unlisted value is a loud TransformError, not a silent
    pass-through or a guessed label — same "never silently guess" rule as
    everywhere else in this tool.
    """
    raw_mapping = transform[len("enum:") :]
    try:
        mapping = json.loads(raw_mapping)
    except json.JSONDecodeError as e:
        raise TransformError(f"enum: invalid JSON mapping {raw_mapping!r}: {e}") from e
    if not isinstance(mapping, dict):
        raise TransformError(f"enum: mapping must be a JSON object, got {type(mapping).__name__}: {raw_mapping!r}")
    key = str(value)
    if key in mapping:
        return mapping[key]
    if "*" in mapping:
        return mapping["*"]
    raise TransformError(
        f"enum: value {value!r} has no entry in the mapping and no \"*\" fallback is set — "
        "add one or the other"
    )


def _apply_split(transform: str, value: Any) -> list:
    """`split:<delimiter>` turns a delimited string into a list — for a
    source field like a comma-separated tag string landing on a Postgres
    ARRAY column. Like every other transform here, this is one field -> one
    column: it does NOT turn one source field into several Postgres columns
    (e.g. splitting "fullName" into separate first_name/last_name columns)
    — the mapping format has no way to express one source field feeding two
    `FieldSpec`s (same limitation `mapping/llm_propose.py` documents from
    the LLM-assist side, where a "split" suggestion is surfaced, not
    applied, for exactly this reason).
    """
    delimiter = transform[len("split:") :]
    if not delimiter:
        raise TransformError("split: needs a non-empty delimiter, e.g. `split:,`")
    if not isinstance(value, str):
        raise TransformError(f"split: expected a string, got {value!r} ({type(value).__name__})")
    return value.split(delimiter)


def apply_default(transform: str | None, value: Any) -> Any:
    """`default:<literal>` only kicks in when the resolved value is None."""
    if value is not None or not transform or not transform.startswith("default:"):
        return value
    return transform.split(":", 1)[1]


def _cast_timestamptz(value: Any) -> datetime.datetime:
    if isinstance(value, datetime.datetime):
        return value if value.tzinfo else value.replace(tzinfo=datetime.UTC)
    if isinstance(value, str):
        parsed = datetime.datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.UTC)
    raise TransformError(f"cast_timestamptz: cannot cast {value!r} ({type(value).__name__})")


def json_safe(value: Any) -> Any:
    """Recursively converts a raw Mongo value into something `json`-native:
    BSON types with no direct JSON equivalent (ObjectId, Decimal128, bytes,
    ...) fall back to `str()`; dict/list recurse; everything already
    JSON-native passes through unchanged. Naive datetimes (what pymongo
    returns — Mongo has no other concept of a stored timezone) are stamped
    UTC explicitly so the resulting JSON value isn't ambiguous to whatever
    reads it later.

    Shared by migrate/load.py (building the `unmapped.jsonb` payload to
    write) and report/validate.py (recomputing the same payload from the
    source document to sample-diff against what actually landed) — one
    definition, so a fix to one path's serialization can't silently drift
    from the other's expectations.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    if isinstance(value, datetime.datetime):
        aware = value if value.tzinfo else value.replace(tzinfo=datetime.UTC)
        return aware.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)
    return str(value)
