"""LEAK#1 — the 3-axis confidence layer. Certification bar: manager tests 1-9.

Confidence is defined on the independent subspace (R2), not naive stdev-of-10. These
prove redundancy immunity, cross-axis gating, deterministic LCB ranking, and that the
point estimate is untouched.
"""

import pytest

from engine.scoring import confidence as C
from src.personas import get_persona_slugs

CORE = ["warren_buffett", "charlie_munger", "radhakishan_damani", "rakesh_jhunjhunwala"]
GROWTH = ["philip_fisher", "peter_lynch"]
VALUE = ["benjamin_graham", "howard_marks", "joel_greenblatt"]
INDEP = ["vijay_kedia"]


def _all(score=8):
    return {s: score for s in get_persona_slugs()}


def _composite(scores):
    return round(sum(scores.values()) / len(scores) * 10, 1)


# 1 — partition integrity
def test_partition_total_and_disjoint():
    slugs = get_persona_slugs()
    assert set(C.PERSONA_AXIS) == set(slugs)          # every persona mapped
    assert len(C.PERSONA_AXIS) == len(slugs) == 10
    for s in slugs:
        assert C.persona_axis(s) in C.AXES            # each maps to a valid axis
    with pytest.raises(ValueError):
        C.persona_axis("nostradamus")                 # unknown slug is a hard error


# 2 — provisional on incomplete axis coverage (redundancy ≠ confidence)
def test_provisional_when_axis_missing():
    scores = {s: 8 for s in CORE + GROWTH}            # missing value + independent
    r = C.compute_confidence(scores, _composite(scores))
    assert r["confidence_tier"] == "provisional"
    assert not C.is_consensus(r)


# 3 — mixed when the value and growth poles diverge
def test_mixed_when_poles_diverge():
    scores = {**{s: 8 for s in CORE + VALUE}, **{s: 2 for s in GROWTH}, "vijay_kedia": 5}
    r = C.compute_confidence(scores, _composite(scores))
    assert r["confidence_tier"] == "mixed"
    assert not r["concordant"]


# 4 — high only on full coverage + concordance + strong magnitude
def test_high_requires_full_concordant_strong():
    scores = _all(8)                                  # all axes, all bullish
    r = C.compute_confidence(scores, _composite(scores))
    assert r["confidence_tier"] == "high"
    assert C.is_consensus(r)
    # weaken magnitude -> drops to moderate (composite near neutral)
    near = _all(5)
    rm = C.compute_confidence(near, 52.0)
    assert rm["confidence_tier"] == "moderate"


# 5 — redundancy immunity: extra correlated-cluster votes never change the tier
def test_redundancy_immune():
    base_others = {**{s: 8 for s in GROWTH + VALUE}, "vijay_kedia": 8}
    four_core = C.compute_confidence({**{s: 8 for s in CORE}, **base_others}, 80.0)
    two_core = C.compute_confidence({**{s: 8 for s in CORE[:2]}, **base_others}, 80.0)
    assert four_core["confidence_tier"] == two_core["confidence_tier"]
    assert four_core["axis_scores"]["core"] == two_core["axis_scores"]["core"]


# 6 — LCB ranks a thin/narrow-axis high score BELOW a cross-axis-concordant lower score
def test_lcb_penalizes_narrow_coverage():
    narrow = C.compute_confidence({"warren_buffett": 7}, 72.0)     # 1 axis, "confident" 72
    broad = C.compute_confidence(_all(7), 68.0)                    # all 4 axes, concordant 68
    assert broad["lcb"] > narrow["lcb"]


# 7 — determinism
def test_deterministic_and_versioned():
    a = C.compute_confidence(_all(6), 60.0)
    b = C.compute_confidence(_all(6), 60.0)
    assert a == b
    assert a["factor_version"] == "axis-v1"


# 8 — "consensus" only on cross-axis agreement
def test_consensus_reserved_for_cross_axis():
    assert C.is_consensus(C.compute_confidence(_all(8), 80.0))          # full + concordant
    assert not C.is_consensus(C.compute_confidence({s: 8 for s in CORE}, 80.0))  # 1 axis
    mixed = C.compute_confidence(
        {**{s: 8 for s in CORE + VALUE}, **{s: 2 for s in GROWTH}, "vijay_kedia": 5}, 50.0
    )
    assert not C.is_consensus(mixed)


# 9 — the point estimate is untouched (layer is purely additive)
def test_point_estimate_untouched():
    r = C.compute_confidence(_all(9), 70.0)
    assert r["magnitude"] == 20.0        # abs(70-50): uses the given composite verbatim
    # no key named 'composite' is produced — the layer never re-derives the number
    assert "composite" not in r
