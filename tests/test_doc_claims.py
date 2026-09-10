"""The docs may not claim a test that does not exist.

The README's status table used to carry ~45 rows marked "✅ — live-tested",
almost all of which were unreproducible notes from a development session
stated as verified fact, complete with specific figures. They were rewritten
to cite the tests that actually back each claim — which only helps for as
long as the citations stay true. A count drifts the moment someone adds a
test, and a file citation survives a rename that a reader never sees.

So the citations are checked here rather than trusted. Notation, enforced by
this file:

    `test_x.py` (12)          x contains exactly 12 tests
    12 ... tests in `test_x.py`   a SUBSET of x — deliberately not the
                                  same shape, so it can never be misread
                                  as a file total

Writing this test immediately caught seven wrong counts in the rewrite that
introduced it, all of them subset counts written in the file-total form.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
TESTS = REPO / "tests"
DOCS = [REPO / "README.md", REPO / "docs" / "engineering-log.md"]

# `test_x.py` optionally followed by (N) — the file-total form.
CITATION = re.compile(r"`((?:integration/)?test_[a-z0-9_]+\.py)`(?:\s*\((\d+)\))?")


def _documents():
    return [(p, p.read_text(encoding="utf-8")) for p in DOCS if p.exists()]


def _count_tests(path: pathlib.Path) -> int:
    """Test functions in a file, counting parametrize cases the way pytest does."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    total = 0
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_"):
            continue
        cases = 1
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            name = ast.unparse(dec.func)
            if "parametrize" in name and len(dec.args) >= 2 and isinstance(dec.args[1], (ast.List, ast.Tuple)):
                cases *= len(dec.args[1].elts)
        total += cases
    return total


def test_the_docs_exist_to_be_checked():
    assert _documents(), "no documentation found to verify"


def test_every_cited_test_file_exists():
    missing = []
    for doc, text in _documents():
        for name, _ in CITATION.findall(text):
            if not (TESTS / name).exists():
                missing.append(f"{doc.name} cites {name}, which does not exist")
    assert not missing, "\n".join(missing)


def test_every_cited_test_count_is_accurate():
    wrong = []
    for doc, text in _documents():
        for name, claimed in CITATION.findall(text):
            if not claimed:
                continue
            path = TESTS / name
            if not path.exists():
                continue  # reported by the test above
            actual = _count_tests(path)
            if int(claimed) != actual:
                wrong.append(f"{doc.name}: `{name}` ({claimed}) — the file has {actual}")
    assert not wrong, (
        "Documented test counts are out of date.\n"
        + "\n".join(wrong)
        + "\n\nIf you meant a SUBSET of the file, write it as "
        '"N ... tests in `file.py`" instead of "`file.py` (N)".'
    )


def test_the_status_table_does_not_reintroduce_unbacked_claims():
    """"live-tested" was the wording that made unverified notes read as proof.

    It is allowed in the engineering log, which discusses the phrase itself
    and labels each entry with its real evidence, but not in the README.
    """
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "live-tested" not in readme, (
        "README reintroduced a 'live-tested' claim. Cite the test that backs it, "
        "or mark it hand-verified in docs/engineering-log.md."
    )


@pytest.mark.parametrize(
    "label",
    [
        "**Automated (CI)**",
        "**Automated against real Mongo + Postgres (CI)**",
        "**Partly automated**",
        "**Hand-verified only — not automated**",
        "**Unverified — no test covers this**",
    ],
)
def test_every_evidence_label_is_defined_in_the_log_preamble(label):
    log = (REPO / "docs" / "engineering-log.md").read_text(encoding="utf-8")
    if label not in log:
        pytest.skip(f"{label} is currently unused")
    preamble = log.split("\n---\n", 1)[0]
    assert label in preamble, f"{label} is used but not explained in the preamble"


def test_every_engineering_log_entry_carries_an_evidence_label():
    log = (REPO / "docs" / "engineering-log.md").read_text(encoding="utf-8")
    body = log.split("\n---\n", 1)[1]
    entries = [e for e in body.split("\n## ")[1:]]
    assert entries, "no entries found in the engineering log"
    unlabelled = [
        e.split("\n", 1)[0][:60]
        for e in entries
        if not any(lbl in e.split("\n\n", 2)[1] for lbl in ("Automated", "Hand-verified", "Unverified", "Partly"))
    ]
    assert not unlabelled, "entries with no evidence label:\n" + "\n".join(unlabelled)


def test_the_headline_test_count_is_a_floor_the_suite_still_clears():
    """The README says "over N unit tests".

    Deliberately a floor rather than an exact figure: an exact number turns
    every added test into a failing build, and the pressure that creates is
    how a number silently stops being true. A floor can only ever be wrong in
    the direction that matters.
    """
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    m = re.search(r"over (\d+)\s+unit\s+tests", readme)
    assert m, 'README no longer states a test-count floor ("over N unit tests")'
    claimed = int(m.group(1))
    actual = sum(_count_tests(p) for p in sorted(TESTS.glob("test_*.py")))
    assert actual >= claimed, (
        f"README claims over {claimed} unit tests; the suite has {actual}"
    )
