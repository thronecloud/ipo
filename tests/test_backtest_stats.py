"""Block-bootstrap confidence intervals for the backtest.

The bootstrap resamples STOCKS (the independent unit), so these tests build
cohorts where the truth is known by construction: a real nonzero effect must
produce a CI that clears zero, pure noise must produce one that straddles it,
and the same seed must reproduce the interval exactly.
"""

from engine.backtest.stats import (
    INDISTINGUISHABLE,
    MIN_STOCKS,
    NEGATIVE,
    POSITIVE,
    bootstrap_ci,
    bootstrap_paths,
    significant,
    verdict,
)
from engine.backtest.study import spearman

SEED = 20260420


def _mean(sample):
    return sum(sample) / len(sample) if sample else None


def test_true_positive_effect_excludes_zero():
    # 30 stocks, every one a positive excess in a tight band -> no resample can
    # drag the mean to or below zero.
    values = [4.0, 5.0, 6.0] * 10
    ci = bootstrap_ci(values, _mean, seed=SEED)
    assert ci is not None
    assert ci["ci_low"] > 0
    assert significant(ci)
    assert ci["verdict"] == POSITIVE


def test_true_negative_effect_excludes_zero():
    values = [-4.0, -5.0, -6.0] * 10
    ci = bootstrap_ci(values, _mean, seed=SEED)
    assert ci["ci_high"] < 0
    assert significant(ci)
    assert ci["verdict"] == NEGATIVE


def test_pure_noise_straddles_zero():
    # Symmetric around zero -> the mean's bootstrap distribution brackets zero.
    values = [-5.0, 5.0] * 15
    ci = bootstrap_ci(values, _mean, seed=SEED)
    assert ci["ci_low"] < 0 < ci["ci_high"]
    assert not significant(ci)
    assert ci["verdict"] == INDISTINGUISHABLE


def test_reproducible_across_runs():
    values = [3.1, -1.2, 0.4, 2.7, -0.9, 1.5, -2.1, 0.8, 1.1, -0.3]
    a = bootstrap_ci(values, _mean, seed=SEED)
    b = bootstrap_ci(values, _mean, seed=SEED)
    assert a == b
    # A different seed gives a different (but valid) interval.
    c = bootstrap_ci(values, _mean, seed=SEED + 1)
    assert c is not None and (a["ci_low"], a["ci_high"]) != (c["ci_low"], c["ci_high"])


def test_below_min_stocks_returns_none():
    assert bootstrap_ci([5.0] * (MIN_STOCKS - 1), _mean, seed=SEED) is None
    assert bootstrap_ci([], _mean, seed=SEED) is None


def test_undefined_point_returns_none():
    # stat_fn that cannot be computed on the cohort -> no CI, not a crash.
    assert bootstrap_ci([1.0, 2.0, 3.0], lambda s: None, seed=SEED) is None


def test_pairwise_unit_keeps_path_together():
    # Each stock is a (score, return) pair; a strongly monotone cohort must land
    # a positive rank-correlation CI. Resampling pairs (not the two columns
    # independently) is what keeps the relationship intact.
    pairs = [(float(i), float(i) + (0.3 if i % 2 else -0.3)) for i in range(20)]

    def _rho(sample):
        return spearman([p[0] for p in sample], [p[1] for p in sample])

    ci = bootstrap_ci(pairs, _rho, seed=SEED)
    assert ci is not None and ci["ci_low"] > 0 and ci["verdict"] == POSITIVE


def test_verdict_thresholds():
    assert verdict(0.1, 0.5) == POSITIVE
    assert verdict(-0.5, -0.1) == NEGATIVE
    assert verdict(-0.2, 0.3) == INDISTINGUISHABLE
    assert verdict(0.0, 0.5) == INDISTINGUISHABLE  # touching zero is not clearing it


def test_significant_handles_none():
    assert significant(None) is False


def test_bootstrap_paths_band_and_min_stocks():
    # Three stocks, coherent per-offset ratios; band brackets the cross-stock
    # mean at each offset. Offset "2" has only two contributing stocks -> no band.
    paths = [
        {0: 1.0, 1: 1.1, 2: 1.2},
        {0: 1.0, 1: 1.2},
        {0: 1.0, 1: 0.9, 2: 0.8},
    ]
    band = bootstrap_paths(paths, [0, 1, 2], seed=SEED)
    lo0, hi0 = band[0]
    assert lo0 == hi0 == 1.0  # every stock is exactly 1.0 at entry
    lo1, hi1 = band[1]
    assert lo1 is not None and lo1 <= hi1
    assert band[2] == (None, None)  # < MIN_STOCKS contributing here
    assert MIN_STOCKS == 3
