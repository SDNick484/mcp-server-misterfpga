"""Keep the assumption registry, the code and the docs in step (the siblings' pattern).

assumptions.py is the source of truth. This fails when:
- an assumption is missing from the README's verification table (or its confidence or status there
  differs), or from HARDWARE_VALIDATION.md
- the docs or code cite an id that isn't in the registry (a typo, or a removed assumption)
- an assumption is cited nowhere in the code or tests (nothing depends on it, so why keep it?)
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from misterfpga_mcp.assumptions import ASSUMPTIONS, BY_ID

ROOT = Path(__file__).parent.parent
ID = re.compile(r"\b[ZMSUH]-[A-Z0-9]+(?:-[A-Z0-9]+)*\b")
DOCS = ("README.md", "HARDWARE_VALIDATION.md")


def cited_in(paths: list[Path]) -> set[str]:
    return {m for path in paths for m in ID.findall(path.read_text())}


def test_ids_are_unique_and_well_formed():
    ids = [a.id for a in ASSUMPTIONS]
    assert len(ids) == len(set(ids))
    assert all(ID.fullmatch(i) for i in ids)


@pytest.mark.parametrize("doc", DOCS)
def test_every_assumption_is_in_each_doc(doc):
    assert set(BY_ID) <= cited_in([ROOT / doc]), set(BY_ID) - cited_in([ROOT / doc])


@pytest.mark.parametrize("doc", DOCS)
def test_docs_cite_only_real_ids(doc):
    assert cited_in([ROOT / doc]) <= set(BY_ID), cited_in([ROOT / doc]) - set(BY_ID)


def test_readme_table_matches_the_registry():
    rows = re.findall(
        r"^\| `([ZMSUH]-[A-Z0-9-]+)`\s*\| (\w+)\s*\| ([\w-]+)\s*\|", (ROOT / "README.md").read_text(), re.M
    )
    assert {r[0]: (r[1], r[2]) for r in rows} == {a.id: (a.confidence, a.status) for a in ASSUMPTIONS}


def test_code_cites_only_real_ids_and_every_id_is_used():
    code = sorted((ROOT / "src").rglob("*.py")) + sorted((ROOT / "tests").glob("test_*.py"))
    code = [p for p in code if p.name not in ("assumptions.py", "test_assumptions.py")]
    cited = {m for p in code for m in re.findall(r"ASSUMPTION ([ZMSUH]-[A-Z0-9-]+)", p.read_text())}
    assert cited <= set(BY_ID), cited - set(BY_ID)
    assert set(BY_ID) <= cited, f"assumptions nothing cites: {set(BY_ID) - cited}"
