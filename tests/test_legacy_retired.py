"""P4: the legacy JSON SUM-scorer is retired.

src/score.py summed persona scores (0-100 = sum of ten 0-10s), which contradicts
the DB composite (mean×10, coverage-independent). Two live scorers with different
formulas means two different "scores" for the same stock — the legacy one must be
gone and nothing may invoke it.
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_legacy_sum_scorer_file_removed():
    assert not (ROOT / "src" / "score.py").exists(), (
        "src/score.py still exists — the SUM formula contradicts the DB mean×10 composite"
    )


def test_nothing_invokes_legacy_scorer():
    offenders = []
    for p in ROOT.rglob("*.py"):
        rel = p.relative_to(ROOT)
        parts = rel.parts
        if parts[0] in (".venv", "web", "node_modules") or "__pycache__" in parts:
            continue
        if rel == pathlib.Path("tests/test_legacy_retired.py"):
            continue
        src = p.read_text()
        if re.search(r"src\.score\b|src/score\.py", src):
            offenders.append(str(rel))
    assert not offenders, f"legacy scorer still referenced by: {offenders}"
