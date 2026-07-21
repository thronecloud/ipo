"""
The confidence layer (LEAK#1).

R2 proved the 10 personas are ~2.3 independent voices (PC1 = 64% of variance): low
dispersion across 10 correlated scores is REDUNDANCY, not agreement. So confidence is
computed on the independent subspace, via a FROZEN partition — never a live PCA fit
(which would drift run-to-run and couple a stock's tier to the rest of the population).

The point estimate (composite = mean(scores)×10) is untouched. This layer adds only a
tier + a rank key; it is purely additive and deterministic.

Four axes (factor_version "axis-v1"), empirically derived from R2's correlation study:
  - core (the correlated quality cluster) collapses to ONE effective vote
  - value pole and growth pole are the real axis of disagreement (Graham/Fisher r=0.13)
  - kedia is the one genuinely independent lens (corr-to-consensus 0.44)
"""

import statistics

FACTOR_VERSION = "axis-v1"

# Frozen partition — every council persona maps to exactly one axis.
PERSONA_AXIS: dict[str, str] = {
    "warren_buffett": "core",
    "charlie_munger": "core",
    "radhakishan_damani": "core",
    "rakesh_jhunjhunwala": "core",
    "philip_fisher": "growth",
    "peter_lynch": "growth",
    "benjamin_graham": "value",
    "howard_marks": "value",
    "joel_greenblatt": "value",
    "vijay_kedia": "independent",
}
AXES = ("core", "growth", "value", "independent")

NEUTRAL = 50.0          # fixed, not a cross-sectional median (determinism)
DEADBAND = 5.0          # 45-55 counts as directionless
MAG_THRESHOLD = 15.0    # |composite-50| for "strong" conviction
K_DISP = 1.0            # LCB dispersion weight
K_COV = 3.0             # LCB per-missing-axis coverage penalty

# A single axis tells us nothing about dispersion. Charging 0.0 credits the thinnest
# possible evidence with perfect confidence; use the population-scale prior instead so
# one opinion is never more certain than a full council.
SOLO_STDERR_PRIOR = 15.0


def persona_axis(slug: str) -> str:
    try:
        return PERSONA_AXIS[slug]
    except KeyError:
        raise ValueError(f"unknown persona slug {slug!r} — not in the frozen axis partition")


def _direction(axis_score: float) -> int:
    if axis_score > NEUTRAL + DEADBAND:
        return 1
    if axis_score < NEUTRAL - DEADBAND:
        return -1
    return 0


def compute_confidence(persona_scores: dict[str, int], composite: float) -> dict:
    """Confidence scorecard from per-persona 0-10 scores + the 0-100 composite.

    Returns axis_scores (0-100 per present axis), the tier, effective dispersion, and
    the LCB rank key — all pure, deterministic functions of THIS stock's own inputs.
    """
    # Collapse the correlated cluster to one vote per axis via the median (outlier-robust).
    by_axis: dict[str, list[int]] = {}
    for slug, score in persona_scores.items():
        by_axis.setdefault(persona_axis(slug), []).append(score)
    axis_scores = {ax: round(statistics.median(v) * 10, 1) for ax, v in by_axis.items()}

    present = [ax for ax in AXES if ax in axis_scores]
    n_present = len(present)
    full_coverage = n_present == len(AXES)

    directions = {_direction(axis_scores[ax]) for ax in present}
    concordant = not (1 in directions and -1 in directions)  # no straddle of neutral
    signed = composite - NEUTRAL
    magnitude = abs(signed)

    if not full_coverage:
        tier = "provisional"
    elif not concordant:
        tier = "mixed"
    elif magnitude < MAG_THRESHOLD:
        tier = "moderate"
    elif signed > 0:
        tier = "high"
    else:
        tier = "high_bearish"

    vals = [axis_scores[ax] for ax in present]
    stderr_eff = (round(statistics.pstdev(vals) / (n_present ** 0.5), 3)
                  if n_present > 1 else SOLO_STDERR_PRIOR)
    # Penalize BOTH real cross-axis disagreement AND thin coverage, so a single-axis
    # "confident-looking" stock cannot outrank a genuine cross-axis agreement.
    lcb = round(composite - K_DISP * stderr_eff - K_COV * (len(AXES) - n_present), 3)

    return {
        "factor_version": FACTOR_VERSION,
        "axis_scores": axis_scores,
        "axes_present": present,
        "concordant": concordant,
        "magnitude": round(magnitude, 1),
        "confidence_tier": tier,
        "score_stderr_eff": stderr_eff,
        "lcb": lcb,
    }


def is_consensus(scorecard: dict) -> bool:
    """The word 'consensus' is permitted ONLY on genuine cross-axis agreement —
    never on low 10-way stdev (which is mere redundancy)."""
    return len(scorecard["axes_present"]) == len(AXES) and scorecard["concordant"]
