"""Point-in-time event study over composite_score_history × daily_prices.

Answers the north-star questions: do high-confidence / high-LCB names outperform
the benchmark? Does LCB rank better than the raw composite? Which picks won?

Honesty rules (violating any of these makes the numbers lies):
- Cohort = EARLIEST history row per stock; entry price is the first close
  STRICTLY AFTER that row's information_date (the day the view formed). No
  same-day fills, no lookahead.
- Survivorship-safe: no status filter anywhere; parked/dead names stay in the
  cohort. Stocks without price data are counted and listed, never dropped.
- Excess return is stock return minus benchmark return over the SAME window,
  matched on calendar dates (benchmark bar on/after entry, on/before exit).
- The coefficients behind lcb (K_DISP, K_COV) are unvalidated priors — this
  study exists to calibrate them, so it reports composite AND lcb ICs side by
  side rather than assuming either works.
"""

from datetime import date, datetime, time, timezone

from sqlalchemy import select

from db.models import Analysis, CompositeScoreHistory, DailyPrice, IndexPrice, Stock
from engine.backtest.execution import (
    ADV_FLOOR,
    Bar,
    ENTRY_WINDOW,
    FRICTION_BPS,
    apply_friction,
    friction_multiplier,
    is_thin,
    resolve_entry,
    trailing_adv,
)
from engine.backtest.stats import bootstrap_ci, bootstrap_paths, verdict
from src.personas import PERSONAS

# Canonical council order — every per-persona result is emitted in this sequence.
COUNCIL = list(PERSONAS.keys())
# Recommendation precedence on a tie (mirrors web/lib/compute.ts so an engine-side
# subset consensus matches what the council selector recomputes client-side).
REC_PRIORITY = ("BUY", "HOLD", "AVOID")

# Nifty 500: the deepest benchmark series Yahoo still updates (BSE-SMLCAP.BO
# froze 2024-05-30; ^CNXSC serves a single bar).
DEFAULT_BENCHMARK = "^CRSLDX"
# Trading-day horizons: ~1w, 1m, 3m, 6m.
DEFAULT_HORIZONS = (5, 21, 63, 126)
QUINTILES = 5
# Bootstrap draws for scalar summary CIs (IC, mean excess, hit rate, spread).
N_BOOT = 2000
# Equity-curve bands re-average every offset on every draw, so a full 2000 over
# a wide horizon is visibly slow; 500 keeps the p5/p95 envelope stable while the
# on-demand endpoint stays snappy.
CURVE_N_BOOT = 500
# Hit rate's null hypothesis is a coin flip vs the benchmark, not zero.
COINFLIP = 50.0

# ── delisting policy ──────────────────────────────────────────────
# Stocks that vanished from the price data are not neutral: delisted names skew
# toward failures, so dropping them flatters the backtest. This POLICY assigns a
# conservative synthetic return to presumed-delisted exclusions so a with-policy
# statistic can be reported alongside the measured-only one. Documented so a
# reader can strip it back out.
#
# A single blunt -100% would over-punish (many "delistings" are symbol changes
# or merges, not zeroes), so the constant is a documented conservative loss and
# merges/unknowns get NO synthetic return at all — they stay excluded-with-reason.
DELISTING_RETURN = -50.0
# status -> presumed outcome. 'stale' is the auto-park state after repeated fetch
# failures (delisting / symbol change, per Stock.status); 'unfetchable' likewise
# never returns data. 'listed_merged' is an explicit merge. Everything else with
# no bars (active/new data gaps, upcoming, withdrawn pre-listing) is 'unknown' —
# not assumed dead.
PRESUMED_DELISTED_STATUSES = frozenset({"unfetchable", "stale"})
PRESUMED_MERGED_STATUSES = frozenset({"listed_merged"})


# ---------- statistics ----------

def _ranks(values: list[float]) -> list[float]:
    """Average ranks (1-based), ties share the mean rank."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Spearman rank correlation; None when undefined (<3 pairs or zero variance)."""
    if len(xs) != len(ys) or len(xs) < 3:
        # With 2 points rank correlation is degenerate (always ±1) — refuse.
        if len(xs) == len(ys) == 2 and len(set(xs)) > 1 and len(set(ys)) > 1:
            return None
        if len(xs) < 3:
            return None
    rx, ry = _ranks(xs), _ranks(ys)
    n = len(rx)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx == 0 or vy == 0:
        return None
    return cov / (vx * vy) ** 0.5


def _pct(a: float, b: float) -> float:
    return (b / a - 1.0) * 100.0


# ---------- data access ----------

def _cohort(session) -> list[tuple]:
    """Earliest history row per stock (the original view), joined to identity.

    DELIBERATELY no Stock.status filter — survivorship invariant.
    """
    first = (
        select(
            CompositeScoreHistory.stock_id,
            CompositeScoreHistory.as_of_date,
            CompositeScoreHistory.id,
        )
        .order_by(
            CompositeScoreHistory.stock_id,
            CompositeScoreHistory.as_of_date.asc(),
            CompositeScoreHistory.id.asc(),
        )
        .distinct(CompositeScoreHistory.stock_id)
        .subquery()
    )
    rows = session.execute(
        select(CompositeScoreHistory, Stock)
        .join(first, first.c.id == CompositeScoreHistory.id)
        .join(Stock, Stock.id == CompositeScoreHistory.stock_id)
        .order_by(Stock.symbol.asc(), CompositeScoreHistory.id.desc())
    ).all()
    return rows


def _price_series(session, stock_ids: list[int]) -> dict[int, list[Bar]]:
    """stock_id -> [Bar] ascending, only bars with a usable close. Carries full
    OHLCV so execution logic can read the open (entry basis), the high/low range
    (circuit detection) and the volume (ADV thinness)."""
    out: dict[int, list[Bar]] = {}
    if not stock_ids:
        return out
    rows = session.execute(
        select(DailyPrice.stock_id, DailyPrice.date, DailyPrice.open,
               DailyPrice.high, DailyPrice.low, DailyPrice.close, DailyPrice.volume)
        .where(DailyPrice.stock_id.in_(stock_ids), DailyPrice.close.is_not(None))
        .order_by(DailyPrice.stock_id, DailyPrice.date.asc(), DailyPrice.id.desc())
    ).all()
    for sid, d, o, hi, lo, close, vol in rows:
        out.setdefault(sid, []).append(Bar(d, o, hi, lo, close, vol))
    return out


def _benchmark_series(session, symbol: str) -> list[tuple[date, float]]:
    return [
        (d, c) for d, c in session.execute(
            select(IndexPrice.date, IndexPrice.close)
            .where(IndexPrice.symbol == symbol, IndexPrice.close.is_not(None))
            .order_by(IndexPrice.date.asc(), IndexPrice.id.desc())
        ).all()
    ]


def _first_on_or_after(series: list[tuple[date, float]], d: date) -> tuple[date, float] | None:
    for row in series:
        if row[0] >= d:
            return row
    return None


def _last_on_or_before(series: list[tuple[date, float]], d: date) -> tuple[date, float] | None:
    prev = None
    for row in series:
        if row[0] > d:
            break
        prev = row
    return prev


def _measure(series: list[Bar], bench: list[tuple[date, float]],
             info_date: date, horizons, friction_bps: float = FRICTION_BPS) -> tuple[int | None, dict]:
    """Entry + forward/excess returns for one stock. Pure; shared by both studies.

    Entry is the first TRADEABLE session strictly after info_date (no lookahead),
    filled at its open (fallback close), skipping circuit-locked sessions up to
    the entry window — see engine.backtest.execution. Alongside the gross
    fields, every return carries a net-of-friction twin (``friction_bps`` charged
    on both legs) and the pick's trailing liquidity (``adv`` / ``thin``).
    Returns (entry_idx, measurement); measurement carries None-filled returns
    when the stock has no tradeable bar after the information date, with
    ``unenterable`` set when bars existed but every one in the window was locked.
    """
    m = {
        "entry_date": None, "entry_price": None, "entry_basis": None,
        "entry_delay_days": 0, "unenterable": False,
        "adv": None, "thin": False,
        "returns": {h: None for h in horizons},
        "excess": {h: None for h in horizons},
        "net_returns": {h: None for h in horizons},
        "net_excess": {h: None for h in horizons},
        "latest_date": None, "latest_price": None,
        "return_to_date": None, "excess_to_date": None,
        "net_return_to_date": None, "net_excess_to_date": None,
    }
    if not series:
        return None, m

    res = resolve_entry(series, info_date)
    if res.status != "ok":
        m["unenterable"] = res.status == "unenterable"
        return None, m

    entry_idx, entry_px = res.index, res.price
    entry_d = series[entry_idx].date
    m["entry_date"], m["entry_price"] = entry_d, entry_px
    m["entry_basis"], m["entry_delay_days"] = res.basis, res.delay
    m["adv"] = trailing_adv(series, entry_idx)
    m["thin"] = is_thin(m["adv"])
    last = series[-1]
    m["latest_date"], m["latest_price"] = last.date, last.close
    if entry_px:
        m["return_to_date"] = _pct(entry_px, last.close)
        m["net_return_to_date"] = apply_friction(m["return_to_date"], friction_bps)

    b_entry = _first_on_or_after(bench, entry_d)
    for h in horizons:
        if entry_idx + h < len(series) and entry_px:
            exit_px = series[entry_idx + h].close
            m["returns"][h] = _pct(entry_px, exit_px)
            m["net_returns"][h] = apply_friction(m["returns"][h], friction_bps)
            if b_entry:
                b_exit = _last_on_or_before(bench, series[entry_idx + h].date)
                bench_move = None
                if b_exit and b_exit[0] > b_entry[0] and b_entry[1]:
                    bench_move = _pct(b_entry[1], b_exit[1])
                elif b_exit and b_entry[1]:
                    # Same-bar window (thin benchmark): 0% benchmark move.
                    bench_move = 0.0
                if bench_move is not None:
                    m["excess"][h] = m["returns"][h] - bench_move
                    m["net_excess"][h] = m["net_returns"][h] - bench_move
    if b_entry and b_entry[1] and m["return_to_date"] is not None:
        b_exit = _last_on_or_before(bench, last.date)
        if b_exit:
            bench_move = _pct(b_entry[1], b_exit[1])
            m["excess_to_date"] = m["return_to_date"] - bench_move
            m["net_excess_to_date"] = m["net_return_to_date"] - bench_move
    return entry_idx, m


# ---------- aggregation ----------

def _mean_of(vals: list[float]) -> float | None:
    return sum(vals) / len(vals) if vals else None


def _hit_of(vals: list[float]) -> float | None:
    return 100.0 * sum(1 for v in vals if v > 0) / len(vals) if vals else None


def _bucket_stats(rows: list[dict], horizons, n_boot: int = N_BOOT) -> dict:
    stats = {
        "n": len(rows),
        "priced": sum(1 for r in rows if r["entry_price"] is not None),
        "thin": sum(1 for r in rows if r.get("thin")),
        "mean_excess": {}, "median_excess": {}, "hit_rate": {}, "mean_return": {},
        "mean_excess_ci": {}, "hit_rate_ci": {},
        # Net-of-friction twin of mean excess (friction charged on both legs).
        "net_mean_excess": {}, "net_mean_excess_ci": {},
    }
    for h in horizons:
        vals = [r["excess"][h] for r in rows if r["excess"].get(h) is not None]
        rets = [r["returns"][h] for r in rows if r["returns"].get(h) is not None]
        net_vals = [r["net_excess"][h] for r in rows if r["net_excess"].get(h) is not None]
        stats["net_mean_excess"][h] = _mean_of(net_vals)
        if vals:
            svals = sorted(vals)
            mid = len(svals) // 2
            stats["mean_excess"][h] = sum(vals) / len(vals)
            stats["median_excess"][h] = (
                svals[mid] if len(svals) % 2 else (svals[mid - 1] + svals[mid]) / 2
            )
            stats["hit_rate"][h] = _hit_of(vals)
        else:
            stats["mean_excess"][h] = None
            stats["median_excess"][h] = None
            stats["hit_rate"][h] = None
        stats["mean_return"][h] = sum(rets) / len(rets) if rets else None

        # Bootstrap the two hypothesis-bearing stats over the stocks. Mean excess
        # is tested against zero (did the basket beat the benchmark?); hit rate
        # against a coin flip (does it beat the benchmark more than half the time?).
        def _excess(sample, _h=h):
            return _mean_of([r["excess"][_h] for r in sample if r["excess"].get(_h) is not None])

        def _hit(sample, _h=h):
            return _hit_of([r["excess"][_h] for r in sample if r["excess"].get(_h) is not None])

        def _net_excess(sample, _h=h):
            return _mean_of([r["net_excess"][_h] for r in sample if r["net_excess"].get(_h) is not None])

        stats["mean_excess_ci"][h] = bootstrap_ci(rows, _excess, n_boot=n_boot)
        stats["net_mean_excess_ci"][h] = bootstrap_ci(rows, _net_excess, n_boot=n_boot)
        hr_ci = bootstrap_ci(rows, _hit, n_boot=n_boot)
        if hr_ci is not None:
            hr_ci["verdict"] = verdict(hr_ci["ci_low"] - COINFLIP, hr_ci["ci_high"] - COINFLIP)
        stats["hit_rate_ci"][h] = hr_ci
    return stats


def _quintile_of(rank_pos: int, n: int) -> int:
    """1..5, 5 = best (highest value). rank_pos is 0-based from LOWEST."""
    return min(QUINTILES, rank_pos * QUINTILES // n + 1)


# ---------- cohort completeness + delisting policy ----------

def _classify(series: list, entry_idx: int | None, meas: dict, horizons) -> str:
    """One of: measured | no_bars | unenterable | bars_predate_view |
    insufficient_forward.

    A partition of the cohort, disjoint from the measurement math. 'measured'
    means the stock contributed at least one forward-horizon return; the four
    exclusion reasons name exactly why the others did not. 'unenterable' is a
    name that traded after the view but was circuit-locked through the entire
    entry window — bars existed, yet no fill was possible.
    """
    if not series:
        return "no_bars"
    if meas.get("unenterable"):
        return "unenterable"
    if entry_idx is None:
        return "bars_predate_view"
    if not any(meas["returns"].get(h) is not None for h in horizons):
        return "insufficient_forward"
    return "measured"


def _presumed_outcome(status: str | None) -> str:
    """delisted | merged | unknown — the fate we impute to an excluded stock."""
    if status in PRESUMED_MERGED_STATUSES:
        return "merged"
    if status in PRESUMED_DELISTED_STATUSES:
        return "delisted"
    return "unknown"


def _cohort_block(records: list[dict], horizons) -> tuple[dict, list[dict]]:
    """Cohort completeness accounting + synthetic policy rows.

    ``records`` is one dict per scored stock: {symbol, status, series,
    entry_idx, meas}. Returns (cohort_block, policy_rows) where policy_rows are
    minimal measurement dicts (entry_price/excess/returns) for the presumed-
    delisted exclusions, ready to fold into a with-policy bucket. Every policy
    application is counted separately so a reader can strip it back out.
    """
    excluded: dict[str, list[str]] = {
        "no_bars": [], "unenterable": [], "bars_predate_view": [],
        "insufficient_forward": [],
    }
    presumed: dict[str, list[str]] = {"delisted": [], "merged": [], "unknown": []}
    applied: dict[str, list[str]] = {
        "constant": [], "actual_last": [], "merged": [], "unknown": [],
    }
    policy_rows: list[dict] = []
    thin: list[str] = []
    measured = priceable = 0

    for rec in records:
        series, entry_idx, meas = rec["series"], rec["entry_idx"], rec["meas"]
        if series:
            priceable += 1
        if meas.get("thin"):
            thin.append(rec["symbol"])
        cls = _classify(series, entry_idx, meas, horizons)
        if cls == "measured":
            measured += 1
            continue

        excluded[cls].append(rec["symbol"])
        outcome = _presumed_outcome(rec["status"])
        presumed[outcome].append(rec["symbol"])

        if outcome == "delisted":
            # A stock that traded AFTER the view has an actual return to its last
            # bar — use it in place of the blunt constant. One that never traded
            # post-view (no_bars / bars_predate_view) gets the documented loss.
            if meas["return_to_date"] is not None:
                applied["actual_last"].append(rec["symbol"])
                ex = (meas["excess_to_date"] if meas["excess_to_date"] is not None
                      else meas["return_to_date"])
                ret = meas["return_to_date"]
            else:
                applied["constant"].append(rec["symbol"])
                ex = ret = DELISTING_RETURN
            net_ex = apply_friction(ex)
            net_ret = apply_friction(ret)
            policy_rows.append({
                "entry_price": meas["entry_price"] if meas["entry_price"] is not None else 1.0,
                "thin": False,
                "excess": {h: ex for h in horizons},
                "returns": {h: ret for h in horizons},
                "net_excess": {h: net_ex for h in horizons},
                "net_returns": {h: net_ret for h in horizons},
            })
        else:
            applied[outcome].append(rec["symbol"])  # merged / unknown: no synthetic return

    block = {
        "scored": len(records),
        "priceable": priceable,
        "measured": measured,
        # Thin overlay (not an exclusion): measured names below the ADV floor,
        # whose printed returns may be unrealizable in size.
        "thin": len(thin),
        "thin_symbols": thin,
        "adv_floor": ADV_FLOOR,
        "excluded": {k: len(v) for k, v in excluded.items()},
        "excluded_symbols": excluded,
        "presumed_outcomes": {k: len(v) for k, v in presumed.items()},
        "presumed_symbols": presumed,
        "policy": {
            "delisting_return": DELISTING_RETURN,
            "applied_constant": len(applied["constant"]),
            "applied_actual_last": len(applied["actual_last"]),
            "excluded_merged": len(applied["merged"]),
            "excluded_unknown": len(applied["unknown"]),
            "symbols": applied,
        },
    }
    return block, policy_rows


# ---------- the study ----------

def run_event_study(session, benchmark: str = DEFAULT_BENCHMARK,
                    horizons=DEFAULT_HORIZONS, n_boot: int = N_BOOT) -> dict:
    horizons = tuple(horizons)
    cohort = _cohort(session)
    bench = _benchmark_series(session, benchmark)
    prices = _price_series(session, [h.stock_id for h, _ in cohort])

    stocks: list[dict] = []
    records: list[dict] = []
    for hist, stock in cohort:
        info_date = (
            hist.information_date.date() if hist.information_date else hist.as_of_date
        )
        series = prices.get(hist.stock_id, [])
        entry_idx, meas = _measure(series, bench, info_date, horizons)

        row = {
            "symbol": stock.symbol,
            "company_name": stock.company_name,
            "status": stock.status,
            "composite": hist.composite_score,
            "lcb": hist.lcb,
            "tier": hist.confidence_tier,
            "recommendation": hist.consensus_recommendation,
            "coverage": hist.analysis_coverage,
            "information_date": info_date,
            **meas,
        }
        stocks.append(row)
        records.append({"symbol": stock.symbol, "status": stock.status,
                        "series": series, "entry_idx": entry_idx, "meas": meas})

    cohort_block, policy_rows = _cohort_block(records, horizons)

    priced_rows = [r for r in stocks if r["entry_price"] is not None]

    # Quintiles over the PRICED cohort (unpriced names cannot rank on returns).
    for key in ("lcb", "composite"):
        ranked = sorted(
            [r for r in priced_rows if r[key] is not None], key=lambda r: r[key]
        )
        n = len(ranked)
        for pos, r in enumerate(ranked):
            r[f"{key}_quintile"] = _quintile_of(pos, n) if n >= QUINTILES else None

    def _group(rows: list[dict], keyfn) -> dict:
        groups: dict = {}
        for r in rows:
            groups.setdefault(keyfn(r), []).append(r)
        return {
            k: _bucket_stats(v, horizons, n_boot=n_boot)
            for k, v in sorted(groups.items(), key=lambda kv: str(kv[0]))
            if k is not None
        }

    def _rho(sample):
        return spearman([p[0] for p in sample], [p[1] for p in sample])

    ic = {"composite": {}, "lcb": {}}
    ic_ci = {"composite": {}, "lcb": {}}
    for h in horizons:
        for key in ("composite", "lcb"):
            pairs = [
                (r[key], r["returns"][h]) for r in priced_rows
                if r[key] is not None and r["returns"].get(h) is not None
            ]
            ic[key][h] = _rho(pairs)
            ic_ci[key][h] = bootstrap_ci(pairs, _rho, n_boot=n_boot)

    thin_free = [r for r in stocks if not r.get("thin")]
    any_thin = len(thin_free) != len(stocks)

    return {
        "benchmark": benchmark,
        "benchmark_bars": len(bench),
        "horizons": list(horizons),
        "cohort_size": len(stocks),
        "priced": len(priced_rows),
        "unpriced_symbols": sorted(r["symbol"] for r in stocks if r["entry_price"] is None),
        # Execution-reality constants, surfaced so the UI can state its terms.
        "friction_bps": FRICTION_BPS,
        "adv_floor": ADV_FLOOR,
        "entry_window": ENTRY_WINDOW,
        "entry_basis": "next_open",
        "stocks": stocks,
        "by_tier": _group(stocks, lambda r: r["tier"]),
        "by_recommendation": _group(stocks, lambda r: r["recommendation"]),
        "by_lcb_quintile": _group(priced_rows, lambda r: r.get("lcb_quintile")),
        "by_composite_quintile": _group(priced_rows, lambda r: r.get("composite_quintile")),
        "ic": ic,
        "ic_ci": ic_ci,
        "overall": _bucket_stats(stocks, horizons, n_boot=n_boot),
        # Cohort completeness is a first-class output: measured vs excluded, with
        # per-reason counts and the presumed fate of the vanished names.
        "cohort": cohort_block,
        "delisting_return_policy": DELISTING_RETURN,
        # Sensitivity guard: the SAME overall stat recomputed with the presumed-
        # delisted names folded back in via the policy. None when no policy row
        # was applied, so a reader can see both the measured-only and the
        # policy-augmented number and the gap between them.
        "overall_with_policy": (
            _bucket_stats(stocks + policy_rows, horizons, n_boot=n_boot)
            if policy_rows else None
        ),
        # Both-ways liquidity guard: the SAME overall stat over the non-thin
        # subset (gross AND net), so a reader sees the headline with and without
        # the names whose returns may be unrealizable in size. None when no thin
        # pick exists (ex-thin would equal overall).
        "overall_ex_thin": (
            _bucket_stats(thin_free, horizons, n_boot=n_boot) if any_thin else None
        ),
    }


# ---------- per-persona study ----------

def _cutoff(hist) -> datetime:
    """Point-in-time boundary for a cohort row: the moment its view formed.

    Only persona verdicts dated on/before this count — no lookahead. Rows that
    predate information_date tracking fall back to the end of their as_of_date.
    """
    if hist.information_date:
        return hist.information_date
    return datetime.combine(hist.as_of_date, time.max, tzinfo=timezone.utc)


def _persona_verdicts(session, cutoffs: dict[int, datetime]) -> dict[int, dict[str, tuple]]:
    """stock_id -> {persona: (score, recommendation)}, latest verdict per persona
    that was known at the stock's cutoff (survivorship- and lookahead-safe)."""
    if not cutoffs:
        return {}
    rows = session.execute(
        select(Analysis.stock_id, Analysis.persona, Analysis.score,
               Analysis.recommendation, Analysis.analyzed_at)
        .where(Analysis.stock_id.in_(list(cutoffs)))
        .order_by(Analysis.stock_id, Analysis.persona,
                  Analysis.analyzed_at.desc(), Analysis.id.desc())
    ).all()
    out: dict[int, dict[str, tuple]] = {}
    seen: set[tuple[int, str]] = set()
    for sid, persona, score, rec, at in rows:
        cutoff = cutoffs.get(sid)
        if cutoff is not None and at is not None and at > cutoff:
            continue
        key = (sid, persona)
        if key in seen:
            continue
        seen.add(key)
        out.setdefault(sid, {})[persona] = (score, rec)
    return out


def _subset_consensus(verdicts: dict[str, tuple], subset) -> tuple[float | None, str | None, int]:
    """(composite 0-100, consensus rec, coverage) over the chosen personas —
    the engine-side twin of web/lib/compute.ts recompute()."""
    scores = [verdicts[p][0] for p in subset
              if p in verdicts and verdicts[p][0] is not None]
    recs = [verdicts[p][1] for p in subset
            if p in verdicts and verdicts[p][1]]
    if not scores:
        return None, None, 0
    composite = round(sum(scores) / len(scores) * 10, 1)
    counts = {r: 0 for r in REC_PRIORITY}
    for r in recs:
        if r in counts:
            counts[r] += 1
    consensus, best = None, -1
    for r in REC_PRIORITY:
        if counts[r] > best:
            best, consensus = counts[r], r
    return composite, (consensus if recs else None), len(scores)


def _mean_excess(rows: list[dict], h) -> float | None:
    vals = [r["excess"][h] for r in rows if r["excess"].get(h) is not None]
    return sum(vals) / len(vals) if vals else None


def _equity_curve(picks: list[dict], bench, offsets, n_boot: int = CURVE_N_BOOT) -> list[dict]:
    """Equal-weighted portfolio value (rebased to 100 at entry) at each
    trading-day offset, alongside the same picks' benchmark path and a p5/p95
    band from resampling the picks. Each pick enters at its own information_date,
    so offsets align by days-held, and the basket shrinks as far-out bars run
    out — every point reports its own count.
    """
    # One rebased-ratio path per pick (None where its bar for that offset is
    # missing); the band resamples these whole paths so it stays coherent.
    pick_paths: list[dict] = []
    for m in picks:
        ei, series = m["entry_idx"], m["series"]
        path: dict = {}
        if ei is not None and series and series[ei].close:
            entry_px = series[ei].close
            for t in offsets:
                if ei + t < len(series):
                    path[t] = series[ei + t].close / entry_px
        pick_paths.append(path)

    band = bootstrap_paths(pick_paths, offsets, n_boot=n_boot)
    net_mult = friction_multiplier()  # scales every gross ratio to its round-trip-net value

    curve = []
    for t in offsets:
        p_ratios = [pp[t] for pp in pick_paths if t in pp]
        b_ratios = []
        for m in picks:
            ei, series = m["entry_idx"], m["series"]
            if ei is None or ei + t >= len(series) or not series[ei].close:
                continue
            b_entry = _first_on_or_after(bench, series[ei].date)
            b_exit = _last_on_or_before(bench, series[ei + t].date)
            if b_entry and b_exit and b_entry[1]:
                b_ratios.append(b_exit[1] / b_entry[1])
        if p_ratios:
            lo, hi = band.get(t, (None, None))
            portfolio = 100 * sum(p_ratios) / len(p_ratios)
            curve.append({
                "t": t,
                "portfolio": round(portfolio, 4),
                # The same basket net of round-trip friction (a constant scaling
                # of the gross value) — the second line on the equity chart.
                "net_portfolio": round(portfolio * net_mult, 4),
                "benchmark": round(100 * sum(b_ratios) / len(b_ratios), 4) if b_ratios else None,
                "n": len(p_ratios),
                "p5": round(100 * lo, 4) if lo is not None else None,
                "p95": round(100 * hi, 4) if hi is not None else None,
            })
    return curve


def _portfolio(members: list[dict], subset, bench, horizons, offsets,
               n_boot: int = N_BOOT, curve_n_boot: int = CURVE_N_BOOT) -> dict:
    """Summary stats + equity curve for the BUY picks of a persona subset,
    plus the BUY-minus-AVOID excess spread (the signal's directional edge)."""
    buys, avoids = [], []
    for m in members:
        cons = _subset_consensus(m["verdicts"], subset)[1]
        if cons == "BUY":
            buys.append(m)
        elif cons == "AVOID":
            avoids.append(m)
    buy_meas = [m["meas"] for m in buys]
    avoid_meas = [m["meas"] for m in avoids]
    spread, spread_ci = {}, {}
    for h in horizons:
        b, a = _mean_excess(buy_meas, h), _mean_excess(avoid_meas, h)
        spread[h] = (b - a) if (b is not None and a is not None) else None
        # Resample the whole picked cohort (BUYs and AVOIDs together, each a
        # stock) and re-measure the gap; a draw missing either side is skipped.
        tagged = ([("BUY", m["excess"].get(h)) for m in buy_meas]
                  + [("AVOID", m["excess"].get(h)) for m in avoid_meas])

        def _spread(sample):
            bv = [v for lab, v in sample if lab == "BUY" and v is not None]
            av = [v for lab, v in sample if lab == "AVOID" and v is not None]
            if not bv or not av:
                return None
            return sum(bv) / len(bv) - sum(av) / len(av)

        spread_ci[h] = bootstrap_ci(tagged, _spread, n_boot=n_boot)
    thin_free = [m for m in buy_meas if not m.get("thin")]
    any_thin = len(thin_free) != len(buy_meas)
    return {
        "n_buy": len(buys),
        "n_avoid": len(avoids),
        "n_buy_priced": sum(1 for m in buys if m["entry_idx"] is not None),
        "n_thin": sum(1 for m in buy_meas if m.get("thin")),
        "stats": _bucket_stats(buy_meas, horizons, n_boot=n_boot),
        # Both-ways: the BUY-pick stats over the non-thin subset (gross AND net).
        # None when no BUY pick is thin.
        "stats_ex_thin": _bucket_stats(thin_free, horizons, n_boot=n_boot) if any_thin else None,
        "spread": spread,
        "spread_ci": spread_ci,
        "curve": _equity_curve(buys, bench, offsets, n_boot=curve_n_boot),
    }


def _normalize_personas(personas) -> list[str]:
    """Validated subset in canonical council order; empty/unknown -> full council."""
    if not personas:
        return list(COUNCIL)
    wanted = set(personas)
    subset = [p for p in COUNCIL if p in wanted]
    return subset or list(COUNCIL)


def run_persona_study(session, benchmark: str = DEFAULT_BENCHMARK,
                      horizons=DEFAULT_HORIZONS, personas=None,
                      n_boot: int = N_BOOT, curve_n_boot: int = CURVE_N_BOOT) -> dict:
    """Per-investor performance: for every council member (and for a chosen
    subset consensus), the forward performance of its top-conviction BUY picks
    vs the benchmark — an equity curve plus hit rate / mean excess / BUY-AVOID
    spread. Same point-in-time cohort and no-lookahead rules as run_event_study.
    """
    horizons = tuple(horizons)
    offsets = list(range(0, max(horizons) + 1)) if horizons else [0]
    subset = _normalize_personas(personas)

    cohort = _cohort(session)
    bench = _benchmark_series(session, benchmark)
    prices = _price_series(session, [h.stock_id for h, _ in cohort])
    verdicts = _persona_verdicts(session, {h.stock_id: _cutoff(h) for h, _ in cohort})

    members: list[dict] = []
    for hist, stock in cohort:
        info_date = (
            hist.information_date.date() if hist.information_date else hist.as_of_date
        )
        series = prices.get(hist.stock_id, [])
        entry_idx, meas = _measure(series, bench, info_date, horizons)
        members.append({
            "symbol": stock.symbol,
            "status": stock.status,
            "verdicts": verdicts.get(hist.stock_id, {}),
            "series": series,
            "entry_idx": entry_idx,
            "meas": meas,
        })

    cohort_block, _ = _cohort_block(members, horizons)

    per_persona = [
        {"persona": slug,
         **_portfolio(members, [slug], bench, horizons, offsets, n_boot, curve_n_boot)}
        for slug in COUNCIL
    ]

    return {
        "benchmark": benchmark,
        "benchmark_bars": len(bench),
        "horizons": list(horizons),
        "curve_offsets": offsets,
        "cohort_size": len(members),
        "cohort": cohort_block,
        "delisting_return_policy": DELISTING_RETURN,
        "friction_bps": FRICTION_BPS,
        "adv_floor": ADV_FLOOR,
        "entry_basis": "next_open",
        "council": list(COUNCIL),
        "personas": per_persona,
        "subset": {"personas": subset,
                   **_portfolio(members, subset, bench, horizons, offsets, n_boot, curve_n_boot)},
    }
