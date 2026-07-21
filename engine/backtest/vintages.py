"""Walk-forward vintages + prompt-era attribution over the event study.

``run_event_study`` (study.py) runs ONCE over the earliest view per stock — a
single frozen experiment. It cannot answer "did v5 fix the signal?" as forward
returns accrue, nor how long a verdict stays informative. This module rolls the
same point-in-time machinery forward through time.

At each formation date T (stepped through the benchmark's trading calendar) we
form the cohort of stocks whose LATEST composite as of T exists — the verdict
known at T, never one dated after it — enter the day after T, and measure
forward returns to the hold horizon versus the benchmark. Every window reports
its own bootstrap CIs (the stock is the resample unit).

Two further axes:
- Era attribution: each window is stratified by the DOMINANT prompt_version and
  by the dominant model behind its composites, so v1/sonnet and v5/fable series
  sit side by side as data accrues. An era with too few measured names in a
  window is reported as insufficient, never as a number.
- IC decay: the same windows yield an IC at several horizons; averaged across
  windows (the window is the resample unit there) it shows how fast a verdict's
  information fades.

Honesty rules are inherited wholesale from study.py — no lookahead (entry is the
first close STRICTLY after T; a verdict dated after T is excluded from window T),
survivorship-safe (no status filter anywhere), excess vs the benchmark over the
matched window.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from datetime import date

from sqlalchemy import select

from db.models import CompositeScoreHistory, Stock
from engine.backtest.stats import bootstrap_ci, verdict
from engine.backtest.study import (
    COINFLIP,
    DEFAULT_BENCHMARK,
    _benchmark_series,
    _hit_of,
    _mean_of,
    _measure,
    _price_series,
    spearman,
)

# Trading-day cadence + hold, matched to the study's horizon vocabulary
# (21 ≈ 1 month, 63 ≈ 3 months). Stepped through the benchmark trading calendar
# so a T is always a real trading date and the spacing is even.
DEFAULT_STEP_DAYS = 21
DEFAULT_HOLD_DAYS = 63
# IC-decay horizons: the same 1w/1m/3m/6m ladder the event study reports.
DECAY_HORIZONS = (5, 21, 63, 126)
# Windows sweep many T's; a full 2000-draw bootstrap on every window and every
# era slice is needlessly slow, so the default is lighter than the study's while
# staying stable. Configurable — callers wanting the study's precision pass 2000.
VINTAGE_N_BOOT = 1000
# An era slice below this many MEASURED names is a fiction, not a data point —
# reported as insufficient rather than a fabricated mean/IC.
MIN_ERA_N = 10


def _dominant(counts: dict | None) -> str:
    """The modal key of a {value: count} provenance map — the era a composite
    predominantly belongs to. Empty/missing -> 'unknown'. Ties break on the key
    so the choice is deterministic."""
    if not counts:
        return "unknown"
    return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]


def _load_history(session) -> dict[int, dict]:
    """stock_id -> {stock, dates: [info_date asc], rows: [hist aligned to dates]}.

    Every history row for every stock, survivorship-safe (no status filter).
    Sorted ascending by information_date so a point-in-time pick at T is a bisect.
    """
    rows = session.execute(
        select(CompositeScoreHistory, Stock)
        .join(Stock, Stock.id == CompositeScoreHistory.stock_id)
    ).all()
    grouped: dict[int, list] = {}
    stocks: dict[int, Stock] = {}
    for hist, stock in rows:
        grouped.setdefault(hist.stock_id, []).append(hist)
        stocks[hist.stock_id] = stock
    out: dict[int, dict] = {}
    for sid, hs in grouped.items():
        hs.sort(key=lambda h: _info_date(h))
        out[sid] = {
            "stock": stocks[sid],
            "dates": [_info_date(h) for h in hs],
            "rows": hs,
        }
    return out


def _info_date(hist) -> date:
    """The date a view formed — its information_date, else its as_of_date."""
    return hist.information_date.date() if hist.information_date else hist.as_of_date


def _pick(entry: dict, cutoff: date):
    """The latest history row with information_date <= cutoff, or None (the view
    had not formed yet at T)."""
    idx = bisect_right(entry["dates"], cutoff)
    if idx == 0:
        return None
    return entry["rows"][idx - 1]


def _window_metrics(members: list[dict], h: int, n_boot: int) -> dict:
    """Mean excess, hit rate and IC (with block-bootstrap CIs) at horizon ``h``
    over ``members`` — the resample unit is the stock. Shared by the whole
    window and by each era slice."""
    excess = [m["meas"]["excess"][h] for m in members if m["meas"]["excess"].get(h) is not None]

    def _ex(sample):
        return _mean_of([m["meas"]["excess"][h] for m in sample
                         if m["meas"]["excess"].get(h) is not None])

    def _hit(sample):
        return _hit_of([m["meas"]["excess"][h] for m in sample
                        if m["meas"]["excess"].get(h) is not None])

    def _ic(sample):
        pairs = [(m["composite"], m["meas"]["returns"][h]) for m in sample
                 if m["composite"] is not None and m["meas"]["returns"].get(h) is not None]
        return spearman([p[0] for p in pairs], [p[1] for p in pairs])

    hit_ci = bootstrap_ci(members, _hit, n_boot=n_boot)
    if hit_ci is not None:
        hit_ci["verdict"] = verdict(hit_ci["ci_low"] - COINFLIP, hit_ci["ci_high"] - COINFLIP)
    return {
        "n": len(excess),
        "mean_excess": _mean_of(excess),
        "mean_excess_ci": bootstrap_ci(members, _ex, n_boot=n_boot),
        "hit_rate": _hit(members),
        "hit_rate_ci": hit_ci,
        "ic": _ic(members),
        "ic_ci": bootstrap_ci(members, _ic, n_boot=n_boot),
    }


def _era_slice(members: list[dict], h: int, n_boot: int, min_era_n: int) -> dict:
    """Metrics for one era's members, or an insufficient-n refusal. Gated on the
    MEASURED count — a mean/IC over fewer than ``min_era_n`` names is not a number
    we will print."""
    measured = sum(1 for m in members if m["meas"]["excess"].get(h) is not None)
    if measured < min_era_n:
        return {"era_n": len(members), "n": measured, "insufficient": True}
    return {"era_n": len(members), "insufficient": False,
            **_window_metrics(members, h, n_boot)}


def _stratify(members: list[dict], axis: str, h: int, n_boot: int, min_era_n: int) -> dict:
    """era value -> slice, grouping members by the given era axis."""
    groups: dict[str, list[dict]] = {}
    for m in members:
        groups.setdefault(m[axis], []).append(m)
    return {
        era: _era_slice(ms, h, n_boot, min_era_n)
        for era, ms in sorted(groups.items())
    }


def _formation_dates(tdates: list[date], earliest: date, step_days: int,
                     hold_days: int) -> list[date]:
    """Formation dates T stepped through the trading calendar: every ``step_days``
    trading days from the first trading date on/after the earliest view, up to the
    last date that still leaves ``hold_days`` trading days of forward room."""
    if not tdates:
        return []
    start = bisect_left(tdates, earliest)  # first trading date on/after the earliest view
    last = len(tdates) - 1 - hold_days
    if last < start:
        return []
    return [tdates[i] for i in range(start, last + 1, step_days)]


def run_vintage_study(session, benchmark: str = DEFAULT_BENCHMARK,
                      step_days: int = DEFAULT_STEP_DAYS,
                      hold_days: int = DEFAULT_HOLD_DAYS,
                      decay_horizons=DECAY_HORIZONS,
                      n_boot: int = VINTAGE_N_BOOT,
                      min_era_n: int = MIN_ERA_N) -> dict:
    """Rolling walk-forward study. See module docstring.

    Returns per-window mean excess / hit rate / IC at the hold horizon (each with
    a bootstrap CI), an era breakdown by dominant prompt_version and model, and an
    IC-decay curve averaged across the same windows.
    """
    decay_horizons = tuple(decay_horizons)
    # Horizons the per-stock measurement must cover: the hold plus every decay rung.
    horizons = tuple(sorted(set((hold_days,)) | set(decay_horizons)))

    history = _load_history(session)
    bench = _benchmark_series(session, benchmark)
    prices = _price_series(session, list(history))
    tdates = [d for d, _ in bench]

    earliest = min((e["dates"][0] for e in history.values()), default=None)
    formation = (
        _formation_dates(tdates, earliest, step_days, hold_days)
        if earliest is not None else []
    )

    windows: list[dict] = []
    for T in formation:
        members: list[dict] = []
        for sid, entry in history.items():
            hist = _pick(entry, T)
            if hist is None:
                continue  # view had not formed at T — no lookahead
            series = prices.get(sid, [])
            entry_idx, meas = _measure(series, bench, T, horizons)
            members.append({
                "symbol": entry["stock"].symbol,
                "composite": hist.composite_score,
                "prompt_era": _dominant(hist.prompt_versions),
                "model_era": _dominant(hist.models_used),
                "meas": meas,
                "entry_idx": entry_idx,
            })

        metrics = _window_metrics(members, hold_days, n_boot)
        ic_by_horizon = {}
        for h in decay_horizons:
            pairs = [(m["composite"], m["meas"]["returns"][h]) for m in members
                     if m["composite"] is not None and m["meas"]["returns"].get(h) is not None]
            ic_by_horizon[h] = spearman([p[0] for p in pairs], [p[1] for p in pairs])

        windows.append({
            "date": T,
            "n_cohort": len(members),
            **metrics,
            "eras": {
                "prompt_version": _stratify(members, "prompt_era", hold_days, n_boot, min_era_n),
                "model": _stratify(members, "model_era", hold_days, n_boot, min_era_n),
            },
            "ic_by_horizon": ic_by_horizon,
        })

    ic_decay = []
    for h in decay_horizons:
        per_window = [w["ic_by_horizon"][h] for w in windows if w["ic_by_horizon"].get(h) is not None]
        ic_decay.append({
            "horizon": h,
            "ic": _mean_of(per_window),
            # The independent unit here is the WINDOW, so the bootstrap resamples
            # windows, not stocks.
            "ic_ci": bootstrap_ci(per_window, _mean_of, n_boot=n_boot) if per_window else None,
            "n_windows": len(per_window),
        })

    return {
        "benchmark": benchmark,
        "benchmark_bars": len(bench),
        "step_days": step_days,
        "hold_days": hold_days,
        "decay_horizons": list(decay_horizons),
        "min_era_n": min_era_n,
        "window_dates": [w["date"] for w in windows],
        "windows": windows,
        "ic_decay": ic_decay,
    }
