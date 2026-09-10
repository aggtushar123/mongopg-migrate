"""`truncate:`, `trim`, and `|` transform pipelines.

These three shipped together and had no tests at all. The pipeline is the
part that actually needs them: `apply_transform` branches on step count, and
`apply_default` deliberately does NOT feed the default through the remaining
steps, so the two interact in a way no single-step test can reach.
"""

from __future__ import annotations

import pytest

from mongopg_migrate.migrate.transform import (
    TransformError,
    apply_default,
    apply_transform,
    split_pipeline,
)

# ── truncate: ────────────────────────────────────────────────────────────────


def test_truncate_caps_an_over_long_value():
    assert apply_transform("truncate:5", "abcdefgh") == "abcde"


def test_truncate_leaves_a_short_value_alone():
    assert apply_transform("truncate:32", "abc") == "abc"


def test_truncate_is_a_no_op_at_exactly_the_limit():
    assert apply_transform("truncate:3", "abc") == "abc"


def test_truncate_passes_none_through():
    # None short-circuits every transform; a NOT NULL column is `default:`'s
    # job, not truncate's.
    assert apply_transform("truncate:3", None) is None


@pytest.mark.parametrize("bad", ["truncate:0", "truncate:-1"])
def test_truncate_rejects_a_non_positive_length(bad):
    with pytest.raises(TransformError, match="must be positive"):
        apply_transform(bad, "abc")


def test_truncate_rejects_a_non_integer_length():
    with pytest.raises(TransformError, match="expected an integer length"):
        apply_transform("truncate:many", "abc")


@pytest.mark.parametrize("value", [123, ["a", "b"], {"a": 1}, True])
def test_truncate_refuses_a_non_string(value):
    # Silently stringifying here is the footgun this project keeps finding:
    # str([1,2]) lands a Python repr in a text column without complaint.
    with pytest.raises(TransformError, match="expected a string"):
        apply_transform("truncate:3", value)


# ── trim ─────────────────────────────────────────────────────────────────────


def test_trim_strips_both_ends():
    assert apply_transform("trim", "  hello  ") == "hello"


def test_trim_preserves_internal_whitespace():
    # Collapsing internal runs would silently rewrite real values; that is a
    # normalisation decision for a lookup table, not a field transform.
    assert apply_transform("trim", "  Liberty  General  ") == "Liberty  General"


def test_trim_handles_the_padding_case_that_motivated_it():
    padded = "180000000000000042" + " " * 48_000
    assert apply_transform("trim", padded) == "180000000000000042"


@pytest.mark.parametrize("value", [42, 3.5, None, ["a"], True])
def test_trim_passes_non_strings_through_untouched(value):
    # Keeps it safe anywhere in a pipeline, e.g. `trim|cast_int`.
    assert apply_transform("trim", value) == value


# ── split_pipeline ───────────────────────────────────────────────────────────


def test_a_spec_with_no_pipe_is_a_one_step_pipeline():
    assert split_pipeline("cast_int") == ["cast_int"]


def test_no_transform_is_an_empty_pipeline():
    assert split_pipeline(None) == []
    assert split_pipeline("") == []


def test_steps_are_stripped_and_empty_steps_dropped():
    assert split_pipeline(" trim | cast_int ") == ["trim", "cast_int"]
    assert split_pipeline("trim||cast_int") == ["trim", "cast_int"]


def test_pipe_inside_an_enum_json_is_not_a_step_boundary():
    # Regression: a naive split("|") cut through the JSON and produced an
    # unterminated-string error naming neither the pipeline nor the pipe —
    # and did so even when enum: was the only transform on the field.
    assert split_pipeline('enum:{"A|B": "both"}') == ['enum:{"A|B": "both"}']


def test_pipe_inside_an_enum_json_string_is_honoured_end_to_end():
    assert apply_transform('enum:{"A|B": "both"}', "A|B") == "both"
    assert apply_transform('enum:{"k": "a|b"}', "k") == "a|b"


def test_escaped_quotes_inside_the_json_do_not_confuse_the_splitter():
    assert apply_transform('enum:{"say \\"hi\\"": "greet"}', 'say "hi"') == "greet"


def test_an_enum_still_composes_with_other_steps():
    assert apply_transform('trim|enum:{"k": "v"}', "  k  ") == "v"
    assert apply_transform('enum:{"k": " v "}|trim', "k") == "v"


# ── pipelines ────────────────────────────────────────────────────────────────


def test_steps_run_left_to_right():
    assert apply_transform("trim|cast_int", "  42  ") == 42


def test_order_matters_and_the_wrong_order_fails_loudly():
    # cast_int first would have to parse "  42  " itself; int() tolerates
    # surrounding whitespace, so pick a case where it genuinely cannot.
    assert apply_transform("trim|truncate:2", "  abcd  ") == "ab"
    assert apply_transform("truncate:2|trim", "  abcd  ") == ""


def test_a_pipeline_short_circuits_entirely_on_none():
    assert apply_transform("trim|truncate:3", None) is None


def test_an_unknown_step_in_a_pipeline_is_rejected():
    with pytest.raises(TransformError, match="unrecognized transform"):
        apply_transform("trim|nonsense", "x")


def test_split_with_a_literal_pipe_delimiter_works_on_its_own():
    # Ambiguous by construction, and the single-step path preserves it.
    assert apply_transform("split:|", "a|b|c") == ["a", "b", "c"]


def test_split_with_a_literal_pipe_in_a_pipeline_explains_the_collision():
    with pytest.raises(TransformError, match="pipeline separator"):
        apply_transform("trim|split:|", " a|b ")


# ── default: inside a pipeline ───────────────────────────────────────────────


def test_default_is_a_pass_through_when_a_value_is_present():
    assert apply_transform("default:X|truncate:3", "abcdef") == "abc"
    assert apply_default("default:X|truncate:3", "abc") == "abc"


def test_default_fires_from_inside_a_pipeline_when_the_value_is_none():
    assert apply_transform("default:NONE|truncate:3", None) is None
    assert apply_default("default:NONE|truncate:3", None) == "NONE"


def test_the_default_literal_is_not_itself_put_through_later_steps():
    """A sharp edge, pinned deliberately.

    `default:` returns its literal verbatim — it is authored to be the final
    value — so a default LONGER than the column is not rescued by a
    `truncate:` in the same pipeline and will still fail the load. Write a
    default that already fits.
    """
    assert apply_default("default:LONGVALUE|truncate:3", None) == "LONGVALUE"


def test_a_bare_default_still_returns_the_raw_literal():
    assert apply_default("default:0", None) == "0"
    assert apply_default("default:", None) == ""


def test_default_ordering_within_the_pipeline_does_not_matter():
    assert apply_default("truncate:3|default:X", None) == "X"
    assert apply_default("default:X|truncate:3", None) == "X"
