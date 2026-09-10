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


# Separator for a transform PIPELINE, e.g. "default:NONE|truncate:32".
# Each step runs left to right on the previous step's output.
TRANSFORM_PIPE = "|"


def split_pipeline(transform: str | None) -> list[str]:
    """Split a transform spec into its steps. A spec with no pipe is a
    one-step pipeline, so every caller can treat the two uniformly."""
    if not transform:
        return []
    return [step.strip() for step in transform.split(TRANSFORM_PIPE) if step.strip()]


def apply_transform(transform: str | None, value: Any) -> Any:
    """Apply a transform spec, which may be a `|`-separated PIPELINE.

    A pipeline exists because a single field can need two unrelated fixes at
    once and `transform:` has only one slot. The case that forced it:
    `hospitals.hl_number` is varchar(32) NOT NULL, one of 64691 source
    documents has no hlNumber at all (needs `default:`), and a different one
    holds a 35-character value (needs `truncate:32`). Either alone leaves the
    other row failing the load, and the field cannot be left unmapped because
    the column is NOT NULL.

    Steps run left to right. `default:` is the exception: it is applied by
    `apply_default` AFTER the rest, since it only fires when the value is
    None and a None short-circuits every other step anyway.
    """
    steps = split_pipeline(transform)
    if len(steps) > 1:
        for step in steps:
            value = _apply_one(step, value)
        return value
    return _apply_one(transform, value)


def _apply_one(transform: str | None, value: Any) -> Any:
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
    if transform.startswith("truncate:"):
        return _apply_truncate(transform, value)
    if transform == "trim":
        return _apply_trim(value)
    raise TransformError(f"unrecognized transform {transform!r} — see migrate/transform.py")


def _apply_trim(value: Any) -> Any:
    """`trim` strips leading and trailing whitespace.

    Deliberately narrow: it removes PADDING, never content, so it is not a
    `truncate:` in disguise and does not need the same "declared loss" framing.
    Internal whitespace is left alone — collapsing that would change values like
    'Liberty  General Insurance' silently, which is a normalisation decision
    belonging in a lookup table, not in a field transform.

    The case that forced it: one source document's reference-number field was
    48607 characters — an 18-character value followed by ~48 KB of trailing
    spaces. The column is varchar(128) and the COPY aborted:

        ERROR: value too long for type character varying(128)

    Widening to hold that would size a column for padding rather than data;
    across all 8548 source values the longest TRIMMED length is 57, which the
    existing varchar(128) already fits. So the honest fix is to drop the
    whitespace, not to store it.

    `report/validate.py` applies the same transform to the source side, so a
    trimmed value is not reported as a mismatch.

    Non-strings pass through untouched, which keeps it safe anywhere in a
    pipeline (`trim|cast_int`, say).
    """
    return value.strip() if isinstance(value, str) else value


def _cast(pytype: type, value: Any, transform: str) -> Any:
    # `int()`/`float()` already reject a list/dict loudly on their own —
    # `bool()` and `str()` don't: `bool([1, 2, 3])` is `True` (any non-empty
    # list is truthy — the exact same footgun class as the character-by-
    # character scalar-iteration bug elsewhere in this tool, found the same
    # way: testing directly rather than assuming), and `str([1, 2, 3])`
    # silently lands the Python repr `'[1, 2, 3]'` in a text column. No
    # transform in this DSL applies element-wise to an array — a Mongo
    # array mapped with no transform at all already lands correctly on a
    # Postgres ARRAY column (psycopg's COPY path adapts Python lists
    # automatically) — so this guard covers all four cast_* transforms
    # uniformly rather than relying on int()/float() happening to raise.
    if isinstance(value, list | dict):
        raise TransformError(
            f"{transform}: cannot cast a {type(value).__name__} ({value!r}) — this transform expects a "
            "scalar value. A Mongo array mapped with no transform at all already lands correctly on a "
            "Postgres ARRAY column; no transform here applies element-wise to an array."
        )
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


def _apply_truncate(transform: str, value: Any) -> str:
    """`truncate:<n>` caps a string at n characters.

    For the case where a `varchar(n)` target is narrower than a handful of
    legacy values and widening the column is not an option (it would diverge
    from the Django model that owns the schema). Without it, a single
    over-long value aborts the entire COPY:

        ERROR: value too long for type character varying(32)
        CONTEXT: COPY accounts, line 438, column reference_code: "..."

    and the field cannot simply be left unmapped when the column is NOT NULL.

    This is deliberately NOT automatic. A transform that silently trimmed
    every over-long value to fit would be precisely the quiet corruption this
    tool exists to prevent — row counts would match while values were subtly
    wrong. Writing `truncate:32` in the mapping makes the loss a declared
    decision, visible in review, at one named field. `validate` applies the
    same transform to the source side, so a truncated value is not reported
    as a mismatch; the mapping file is the record of what was given up.
    """
    raw = transform[len("truncate:") :]
    try:
        limit = int(raw)
    except ValueError as e:
        raise TransformError(f"truncate: expected an integer length, got {raw!r}") from e
    if limit <= 0:
        raise TransformError(f"truncate: length must be positive, got {limit}")
    if not isinstance(value, str):
        raise TransformError(f"truncate: expected a string, got {value!r} ({type(value).__name__})")
    return value[:limit]


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
    """`default:<literal>` only kicks in when the resolved value is None.

    Also finds a `default:` step inside a PIPELINE ("default:X|truncate:32"),
    so the two compose. The literal is returned verbatim, exactly as before —
    it is deliberately NOT fed through the pipeline's remaining steps, because
    a default is authored to be the final value already (and tests pin the raw
    string form: apply_default("default:0", None) == "0").
    """
    if value is not None or not transform:
        return value
    for step in split_pipeline(transform):
        if step.startswith("default:"):
            return step.split(":", 1)[1]
    return value


def _cast_timestamptz(value: Any) -> datetime.datetime:
    if isinstance(value, datetime.datetime):
        return value if value.tzinfo else value.replace(tzinfo=datetime.UTC)
    if isinstance(value, str):
        # `fromisoformat` raises a bare ValueError on anything it dislikes —
        # the empty string included, which legacy exports are full of. That
        # escaped this module entirely and killed the whole run with a raw
        # traceback, instead of being reported as a per-field violation the way
        # every other failed cast is (`_cast` already wraps its own errors).
        # Dry-run especially is meant to COLLECT these: crashing on the first
        # one hides every other problem behind it.
        try:
            parsed = datetime.datetime.fromisoformat(value)
        except ValueError as e:
            raise TransformError(f"cast_timestamptz: cannot cast {value!r} (str): {e}") from e
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
