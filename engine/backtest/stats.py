"""Block bootstrap over stocks for backtest confidence intervals.

Every backtest headline currently ships as a bare point estimate over a
cross-sectionally correlated cohort. A naive i.i.d. bootstrap would understate
the noise, because two stocks' return paths move together. So we resample the
STOCKS (the independent-ish unit) with replacement, keeping each stock's full
contribution intact — its whole return path travels as one block — and take
percentile CIs of the recomputed statistic.

Seeds are fixed and configurable, never wall-clock or os.urandom: the same
cohort and seed reproduce the same interval bar-for-bar, the same determinism
discipline as the rest of the engine.
"""

from __future__ import annotations

import random
from typing import Callable, Sequence

# Default bootstrap draws for scalar statistics. Curve bands sweep every offset
# on every draw, so callers pass a smaller count there (see CURVE_N_BOOT in the
# study module) — kept configurable so both can be tuned.
DEFAULT_N_BOOT = 2000
DEFAULT_ALPHA = 0.05
# 2026-04-20: the pilot cohort's formation date. A fixed, meaningful seed so a
# published interval is reproducible and auditable.
DEFAULT_SEED = 20260420
# Below this a bootstrap CI is a fiction (a 1-stock "interval" is a point). We
# report no CI rather than a fake-tight one — honesty over coverage.
MIN_STOCKS = 3

POSITIVE = "positive"
NEGATIVE = "negative"
INDISTINGUISHABLE = "indistinguishable from zero"


def verdict(ci_low: float, ci_high: float) -> str:
    """One word for where the interval sits relative to zero.

    "positive"/"negative" only when the whole interval clears zero; otherwise
    the effect is "indistinguishable from zero" — the honest default.
    """
    if ci_low > 0:
        return POSITIVE
    if ci_high < 0:
        return NEGATIVE
    return INDISTINGUISHABLE


def significant(ci: dict | None) -> bool:
    """True when the CI excludes zero (both bounds share a sign)."""
    if not ci:
        return False
    return ci["ci_low"] > 0 or ci["ci_high"] < 0


def _percentile(sorted_vals: list[float], p: float) -> float:
    """Linear-interpolated percentile (p in [0, 100]) of an ascending list."""
    n = len(sorted_vals)
    if n == 1:
        return sorted_vals[0]
    rank = (p / 100.0) * (n - 1)
    lo = int(rank)
    if lo + 1 >= n:
        return sorted_vals[-1]
    frac = rank - lo
    return sorted_vals[lo] + frac * (sorted_vals[lo + 1] - sorted_vals[lo])


def bootstrap_ci(
    values_by_stock: Sequence,
    stat_fn: Callable[[Sequence], float | None],
    n_boot: int = DEFAULT_N_BOOT,
    alpha: float = DEFAULT_ALPHA,
    seed: int = DEFAULT_SEED,
) -> dict | None:
    """Percentile CI for ``stat_fn`` under resampling of stocks with replacement.

    ``values_by_stock`` holds one item per stock — the independent unit. Each
    item carries whatever ``stat_fn`` needs (a scalar excess, a (score, return)
    pair, a (label, value) tuple…). A draw resamples the stocks with
    replacement, keeps each drawn item whole, and recomputes ``stat_fn`` on the
    resampled cohort. Returns ``{point, ci_low, ci_high, verdict, n}`` — or
    ``None`` when the point estimate is undefined or the cohort is below
    ``MIN_STOCKS`` (a CI there would be a fiction).
    """
    items = list(values_by_stock)
    n = len(items)
    point = stat_fn(items)
    if point is None or n < MIN_STOCKS:
        return None

    rng = random.Random(seed)
    boots: list[float] = []
    for _ in range(n_boot):
        sample = [items[rng.randrange(n)] for _ in range(n)]
        v = stat_fn(sample)
        if v is not None:
            boots.append(v)
    if not boots:
        return None

    boots.sort()
    lo = _percentile(boots, 100.0 * (alpha / 2.0))
    hi = _percentile(boots, 100.0 * (1.0 - alpha / 2.0))
    return {
        "point": point,
        "ci_low": lo,
        "ci_high": hi,
        "verdict": verdict(lo, hi),
        "n": n,
    }


def bootstrap_paths(
    paths_by_stock: Sequence[dict],
    keys: Sequence,
    lo_pct: float = 5.0,
    hi_pct: float = 95.0,
    n_boot: int = DEFAULT_N_BOOT,
    seed: int = DEFAULT_SEED,
) -> dict:
    """Per-key percentile envelope of the cross-stock mean, resampling stocks.

    ``paths_by_stock`` is one dict per stock mapping key -> value (a stock's
    ratio at each trading-day offset). One draw resamples the whole cohort of
    stocks with replacement and, for every ``key``, averages the drawn stocks
    that have a value there — so the band is coherent (one resample yields one
    whole path). Returns ``{key: (lo, hi)}``; a key with fewer than
    ``MIN_STOCKS`` contributing stocks gets ``(None, None)``.
    """
    stocks = list(paths_by_stock)
    n = len(stocks)
    out: dict = {k: (None, None) for k in keys}
    if n < MIN_STOCKS:
        return out

    rng = random.Random(seed)
    dists: dict = {k: [] for k in keys}
    counts: dict = {k: 0 for k in keys}
    for k in keys:
        counts[k] = sum(1 for s in stocks if s.get(k) is not None)
    for _ in range(n_boot):
        sample = [stocks[rng.randrange(n)] for _ in range(n)]
        for k in keys:
            vals = [s[k] for s in sample if s.get(k) is not None]
            if vals:
                dists[k].append(sum(vals) / len(vals))
    for k in keys:
        if counts[k] < MIN_STOCKS or not dists[k]:
            continue
        vs = sorted(dists[k])
        out[k] = (_percentile(vs, lo_pct), _percentile(vs, hi_pct))
    return out
