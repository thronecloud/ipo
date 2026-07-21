"""Factor attribution — does the composite pick winners, or just tilt?

Every backtest headline is measured against ONE smallcap benchmark. A score that
quietly loads on a size or sector bet would beat that benchmark without picking a
single stock well — the tilt masquerades as skill. This module separates the two.

Cross-sectionally, over the measured cohort at a chosen horizon, we regress each
stock's EXCESS return on [intercept, a size term (centred log market cap), sector
dummies]. The fitted intercept is the residual alpha; the residuals are the
excess returns with the size and sector tilt stripped out. The composite's rank
correlation against those residuals (the tilt-stripped IC) sits beside its raw IC
against the excess returns — if the raw IC survives the strip, the score is
picking; if it collapses, it was riding a tilt.

Honesty rules:
- Sectors with fewer than MIN_SECTOR_FOR_DUMMY names pool into "other", and only
  the top TOP_SECTORS by count get their own dummy — a rank-deficient design is a
  lie, not a model. One group is held out as the reference (baseline) so the
  intercept + dummies are not collinear (the dummy-variable trap).
- If the design is STILL singular after pooling (e.g. size is collinear with the
  sector split), the dummies are dropped and the payload says so. If size itself
  is degenerate it is dropped too, down to an intercept-only fit.
- Below MIN_ATTRIBUTION_STOCKS eligible names the whole block refuses rather than
  fit noise; below MIN_SECTOR_STOCKS a per-sector IC reports insufficient. Same
  refusal discipline as the rest of the engine.
- The OLS is solved in pure Python (stdlib only — no numpy in this image), fine
  for the tiny design matrices here (~10 columns).
"""

from __future__ import annotations

import math

from engine.backtest.stats import DEFAULT_N_BOOT, DEFAULT_SEED, bootstrap_ci
from engine.backtest.study import spearman

# A named sector needs at least this many names to earn its own dummy; below it
# pools into "other" (a lone-stock dummy explains only itself — pure overfit).
MIN_SECTOR_FOR_DUMMY = 3
# At most this many sectors carry their own dummy; the long tail pools into
# "other". Keeps the design matrix short over a fat-tailed sector distribution.
TOP_SECTORS = 8
# Below this many eligible names (score + market cap + horizon return) the whole
# attribution refuses — a factor fit on a handful of stocks is a fiction.
MIN_ATTRIBUTION_STOCKS = 8
# A per-sector IC needs at least this many measured names to be reported.
MIN_SECTOR_STOCKS = 8
# A pivot below this fraction of the matrix scale is treated as zero — the
# rank-deficiency guard. Exact dummy collinearity lands orders of magnitude
# under this; the tolerance only has to separate it from honest small pivots.
SINGULAR_TOL = 1e-9


# ---------- pure-Python linear algebra ----------

def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def _solve(a: list[list[float]], b: list[float]) -> list[float] | None:
    """Solve ``a x = b`` by Gauss-Jordan elimination with partial pivoting.

    ``a`` is square (n×n). Returns the solution vector, or None when the matrix
    is singular to tolerance (a pivot below SINGULAR_TOL × the matrix scale) —
    the caller reads None as rank-deficiency and sheds columns.
    """
    n = len(a)
    m = [list(a[i]) + [b[i]] for i in range(n)]
    scale = max((abs(m[i][j]) for i in range(n) for j in range(n)), default=0.0) or 1.0
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < SINGULAR_TOL * scale:
            return None
        m[col], m[pivot] = m[pivot], m[col]
        pv = m[col][col]
        for r in range(n):
            if r == col:
                continue
            f = m[r][col] / pv
            if f:
                for c in range(col, n + 1):
                    m[r][c] -= f * m[col][c]
    return [m[i][n] / m[i][i] for i in range(n)]


def _ols(x: list[list[float]], y: list[float]) -> tuple[list[float], list[float], float | None] | None:
    """Ordinary least squares via the normal equations (XᵀX)β = Xᵀy.

    Returns ``(beta, residuals, r2)`` — coefficients in the column order of ``x``,
    the per-row residuals, and R² (None when the response has zero variance). None
    when XᵀX is singular. Design matrices here are small, so the normal equations
    (whose conditioning is the squared design conditioning) are entirely adequate;
    exact collinearity — the case we must catch — is unmissable at any precision.
    """
    n = len(x)
    k = len(x[0])
    ata = [[sum(x[r][i] * x[r][j] for r in range(n)) for j in range(k)] for i in range(k)]
    atb = [sum(x[r][i] * y[r] for r in range(n)) for i in range(k)]
    beta = _solve(ata, atb)
    if beta is None:
        return None
    fitted = [sum(x[r][i] * beta[i] for i in range(k)) for r in range(n)]
    resid = [y[r] - fitted[r] for r in range(n)]
    ybar = _mean(y)
    ss_tot = sum((v - ybar) ** 2 for v in y)
    ss_res = sum(e * e for e in resid)
    r2 = None if ss_tot == 0 else 1.0 - ss_res / ss_tot
    return beta, resid, r2


# ---------- attribution ----------

def _sector_label(row: dict) -> str:
    """A stock's sector, with the missing/blank case folded into 'other'."""
    return (row.get("sector") or "other").strip() or "other"


def _rho_of_pairs(sample) -> float | None:
    return spearman([p[0] for p in sample], [p[1] for p in sample])


def _refusal(horizon: int, n: int, measured: int, reason: str) -> dict:
    """Null-filled block when the cohort is too thin to attribute honestly."""
    return {
        "horizon": horizon,
        "n": n,
        "measured": measured,
        "insufficient": True,
        "reason": reason,
        "dummies_dropped": False,
        "size_dropped": False,
        "reference_sector": None,
        "modeled_sectors": [],
        "pooled_into_other": [],
        "alpha": None,
        "size_loading": None,
        "sector_loadings": {},
        "r2": None,
        "raw_ic": None,
        "residual_ic": None,
        "per_sector_ic": {},
    }


def run_attribution(rows: list[dict], horizon: int,
                    n_boot: int = DEFAULT_N_BOOT, seed: int = DEFAULT_SEED) -> dict:
    """Cross-sectional factor attribution over the measured cohort at ``horizon``.

    Each row is one scored stock carrying ``composite`` (0-100), ``sector``,
    ``mcap`` (market cap, any consistent unit — only its log matters), and the
    ``excess`` / ``returns`` horizon dicts from the event study. Returns the
    attribution block: residual alpha, size and sector loadings, R², the raw and
    tilt-stripped ICs with bootstrap CIs, and per-sector ICs. See the module
    docstring for the honesty rules that govern refusals and column-shedding.
    """
    h = horizon

    # Per-sector IC works off the MEASURED cohort (score + horizon return) — it
    # does not need market cap, so it is not gated on it.
    measured = [
        r for r in rows
        if r.get("composite") is not None and r.get("returns", {}).get(h) is not None
    ]

    # The regression additionally needs a positive market cap (for its log) and a
    # horizon EXCESS return (the regressand is excess, not raw).
    elig = [
        r for r in measured
        if r.get("mcap") is not None and r["mcap"] > 0
        and r.get("excess", {}).get(h) is not None
    ]
    n = len(elig)

    per_sector = _per_sector_ic(measured, h, n_boot, seed)

    if n < MIN_ATTRIBUTION_STOCKS:
        block = _refusal(
            h, n, len(measured),
            f"only {n} name(s) have a score, a market cap and a {h}-day excess "
            f"return (need {MIN_ATTRIBUTION_STOCKS} to attribute)",
        )
        block["per_sector_ic"] = per_sector
        return block

    # ---- sector grouping: pool the tail, keep the top few ----
    counts: dict[str, int] = {}
    for r in elig:
        counts[_sector_label(r)] = counts.get(_sector_label(r), 0) + 1
    named = {s for s in counts if s != "other"}
    eligible_named = {s for s in named if counts[s] >= MIN_SECTOR_FOR_DUMMY}
    top = sorted(eligible_named, key=lambda s: (-counts[s], s))[:TOP_SECTORS]
    top_set = set(top)
    pooled_into_other = sorted(named - top_set)

    def group(r: dict) -> str:
        s = _sector_label(r)
        return s if s in top_set else "other"

    grp = [group(r) for r in elig]
    grp_counts: dict[str, int] = {}
    for g in grp:
        grp_counts[g] = grp_counts.get(g, 0) + 1
    present = sorted(grp_counts, key=lambda s: (-grp_counts[s], s))
    # Hold one group out as the reference (baseline) to dodge the dummy trap:
    # prefer the pooled "other", else the largest present group.
    reference = "other" if "other" in grp_counts else present[0]
    dummy_sectors = [g for g in present if g != reference]

    # ---- design columns: intercept, centred log size, sector dummies ----
    logm = [math.log(r["mcap"]) for r in elig]
    mbar = _mean(logm)
    size = [v - mbar for v in logm]
    use_size = sum(v * v for v in size) > 1e-12  # log size actually varies
    y = [r["excess"][h] for r in elig]

    def build(with_dummies: bool, with_size: bool) -> list[list[float]]:
        x = []
        for i, r in enumerate(elig):
            rowv = [1.0]
            if with_size:
                rowv.append(size[i])
            if with_dummies:
                rowv.extend(1.0 if grp[i] == ds else 0.0 for ds in dummy_sectors)
            x.append(rowv)
        return x

    # ---- fit, shedding columns on rank-deficiency ----
    final_dummies = bool(dummy_sectors)
    final_size = use_size
    dummies_dropped = False
    size_dropped = not use_size

    res = _ols(build(final_dummies, final_size), y)
    if res is None and final_dummies:
        final_dummies, dummies_dropped = False, True
        res = _ols(build(final_dummies, final_size), y)
    if res is None and final_size:
        final_size, size_dropped = False, True
        res = _ols(build(final_dummies, final_size), y)
    if res is None:
        # Intercept-only: XᵀX = [[n]], never singular.
        final_dummies, final_size = False, False
        res = _ols(build(False, False), y)

    beta, resid, r2 = res
    idx = 0
    alpha = beta[idx]
    idx += 1
    size_loading = None
    if final_size:
        size_loading = beta[idx]
        idx += 1
    sector_loadings: dict[str, float] = {}
    if final_dummies:
        for ds in dummy_sectors:
            sector_loadings[ds] = beta[idx]
            idx += 1

    # ---- raw vs tilt-stripped IC, both bootstrapped over the SAME cohort ----
    comps = [r["composite"] for r in elig]
    raw_ic = bootstrap_ci(list(zip(comps, y)), _rho_of_pairs, n_boot=n_boot, seed=seed)
    residual_ic = bootstrap_ci(list(zip(comps, resid)), _rho_of_pairs, n_boot=n_boot, seed=seed)

    return {
        "horizon": h,
        "n": n,
        "measured": len(measured),
        "insufficient": False,
        "reason": None,
        "dummies_dropped": dummies_dropped,
        "size_dropped": size_dropped,
        "reference_sector": reference,
        "modeled_sectors": dummy_sectors if final_dummies else [],
        "pooled_into_other": pooled_into_other,
        "alpha": alpha,
        "size_loading": size_loading,
        "sector_loadings": sector_loadings,
        "r2": r2,
        "raw_ic": raw_ic,
        "residual_ic": residual_ic,
        "per_sector_ic": per_sector,
    }


def _per_sector_ic(measured: list[dict], h: int, n_boot: int, seed: int) -> dict:
    """Composite-vs-return Spearman within each sector, ordered by count.

    A sector with fewer than MIN_SECTOR_STOCKS measured names reports
    ``{insufficient, n}`` rather than a correlation the data can't support.
    """
    by_sec: dict[str, list[dict]] = {}
    for r in measured:
        by_sec.setdefault(_sector_label(r), []).append(r)

    out: dict[str, dict] = {}
    for sec, members in sorted(by_sec.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        pairs = [(m["composite"], m["returns"][h]) for m in members]
        if len(pairs) < MIN_SECTOR_STOCKS:
            out[sec] = {"insufficient": True, "n": len(pairs)}
            continue
        ci = bootstrap_ci(pairs, _rho_of_pairs, n_boot=n_boot, seed=seed)
        out[sec] = ci if ci is not None else {"insufficient": True, "n": len(pairs)}
    return out
