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


def _price_series(session, stock_ids: list[int]) -> dict[int, list[tuple[date, float]]]:
    """stock_id -> [(date, close)] ascending, only bars with a usable close."""
    out: dict[int, list[tuple[date, float]]] = {}
    if not stock_ids:
        return out
    rows = session.execute(
        select(DailyPrice.stock_id, DailyPrice.date, DailyPrice.close)
        .where(DailyPrice.stock_id.in_(stock_ids), DailyPrice.close.is_not(None))
        .order_by(DailyPrice.stock_id, DailyPrice.date.asc(), DailyPrice.id.desc())
    ).all()
    for sid, d, close in rows:
        out.setdefault(sid, []).append((d, close))
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


def _measure(series: list[tuple[date, float]], bench: list[tuple[date, float]],
             info_date: date, horizons) -> tuple[int | None, dict]:
    """Entry + forward/excess returns for one stock. Pure; shared by both studies.

    Entry = first close STRICTLY after info_date (no lookahead). Returns
    (entry_idx, measurement); measurement carries None-filled returns when the
    stock has no usable bar after the information date.
    """
    m = {
        "entry_date": None, "entry_price": None,
        "returns": {h: None for h in horizons},
        "excess": {h: None for h in horizons},
        "latest_date": None, "latest_price": None,
        "return_to_date": None, "excess_to_date": None,
    }
    entry_idx = next((i for i, (d, _) in enumerate(series) if d > info_date), None)
    if entry_idx is None:
        return None, m

    entry_d, entry_px = series[entry_idx]
    m["entry_date"], m["entry_price"] = entry_d, entry_px
    last_d, last_px = series[-1]
    m["latest_date"], m["latest_price"] = last_d, last_px
    if entry_px:
        m["return_to_date"] = _pct(entry_px, last_px)

    b_entry = _first_on_or_after(bench, entry_d)
    for h in horizons:
        if entry_idx + h < len(series) and entry_px:
            exit_d, exit_px = series[entry_idx + h]
            m["returns"][h] = _pct(entry_px, exit_px)
            if b_entry:
                b_exit = _last_on_or_before(bench, exit_d)
                if b_exit and b_exit[0] > b_entry[0] and b_entry[1]:
                    m["excess"][h] = m["returns"][h] - _pct(b_entry[1], b_exit[1])
                elif b_exit and b_entry[1]:
                    # Same-bar window (thin benchmark): 0% benchmark move.
                    m["excess"][h] = m["returns"][h]
    if b_entry and b_entry[1] and m["return_to_date"] is not None:
        b_exit = _last_on_or_before(bench, last_d)
        if b_exit:
            m["excess_to_date"] = m["return_to_date"] - _pct(b_entry[1], b_exit[1])
    return entry_idx, m


# ---------- aggregation ----------

def _bucket_stats(rows: list[dict], horizons) -> dict:
    stats = {
        "n": len(rows),
        "priced": sum(1 for r in rows if r["entry_price"] is not None),
        "mean_excess": {}, "median_excess": {}, "hit_rate": {}, "mean_return": {},
    }
    for h in horizons:
        vals = [r["excess"][h] for r in rows if r["excess"].get(h) is not None]
        rets = [r["returns"][h] for r in rows if r["returns"].get(h) is not None]
        if vals:
            svals = sorted(vals)
            mid = len(svals) // 2
            stats["mean_excess"][h] = sum(vals) / len(vals)
            stats["median_excess"][h] = (
                svals[mid] if len(svals) % 2 else (svals[mid - 1] + svals[mid]) / 2
            )
            stats["hit_rate"][h] = 100.0 * sum(1 for v in vals if v > 0) / len(vals)
        else:
            stats["mean_excess"][h] = None
            stats["median_excess"][h] = None
            stats["hit_rate"][h] = None
        stats["mean_return"][h] = sum(rets) / len(rets) if rets else None
    return stats


def _quintile_of(rank_pos: int, n: int) -> int:
    """1..5, 5 = best (highest value). rank_pos is 0-based from LOWEST."""
    return min(QUINTILES, rank_pos * QUINTILES // n + 1)


# ---------- the study ----------

def run_event_study(session, benchmark: str = DEFAULT_BENCHMARK,
                    horizons=DEFAULT_HORIZONS) -> dict:
    horizons = tuple(horizons)
    cohort = _cohort(session)
    bench = _benchmark_series(session, benchmark)
    prices = _price_series(session, [h.stock_id for h, _ in cohort])

    stocks: list[dict] = []
    for hist, stock in cohort:
        info_date = (
            hist.information_date.date() if hist.information_date else hist.as_of_date
        )
        series = prices.get(hist.stock_id, [])
        _, meas = _measure(series, bench, info_date, horizons)

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
            k: _bucket_stats(v, horizons)
            for k, v in sorted(groups.items(), key=lambda kv: str(kv[0]))
            if k is not None
        }

    ic = {"composite": {}, "lcb": {}}
    for h in horizons:
        for key in ("composite", "lcb"):
            pairs = [
                (r[key], r["returns"][h]) for r in priced_rows
                if r[key] is not None and r["returns"].get(h) is not None
            ]
            ic[key][h] = spearman([p[0] for p in pairs], [p[1] for p in pairs])

    return {
        "benchmark": benchmark,
        "benchmark_bars": len(bench),
        "horizons": list(horizons),
        "cohort_size": len(stocks),
        "priced": len(priced_rows),
        "unpriced_symbols": sorted(r["symbol"] for r in stocks if r["entry_price"] is None),
        "stocks": stocks,
        "by_tier": _group(stocks, lambda r: r["tier"]),
        "by_recommendation": _group(stocks, lambda r: r["recommendation"]),
        "by_lcb_quintile": _group(priced_rows, lambda r: r.get("lcb_quintile")),
        "by_composite_quintile": _group(priced_rows, lambda r: r.get("composite_quintile")),
        "ic": ic,
        "overall": _bucket_stats(stocks, horizons),
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


def _equity_curve(picks: list[dict], bench, offsets) -> list[dict]:
    """Equal-weighted portfolio value (rebased to 100 at entry) at each
    trading-day offset, alongside the same picks' benchmark path. Each pick
    enters at its own information_date, so offsets align by days-held, and the
    basket shrinks as far-out bars run out — every point reports its own count.
    """
    curve = []
    for t in offsets:
        p_ratios, b_ratios = [], []
        for m in picks:
            ei, series = m["entry_idx"], m["series"]
            if ei is None or ei + t >= len(series):
                continue
            entry_px = series[ei][1]
            if not entry_px:
                continue
            exit_d, exit_px = series[ei + t]
            p_ratios.append(exit_px / entry_px)
            b_entry = _first_on_or_after(bench, series[ei][0])
            b_exit = _last_on_or_before(bench, exit_d)
            if b_entry and b_exit and b_entry[1]:
                b_ratios.append(b_exit[1] / b_entry[1])
        if p_ratios:
            curve.append({
                "t": t,
                "portfolio": round(100 * sum(p_ratios) / len(p_ratios), 4),
                "benchmark": round(100 * sum(b_ratios) / len(b_ratios), 4) if b_ratios else None,
                "n": len(p_ratios),
            })
    return curve


def _portfolio(members: list[dict], subset, bench, horizons, offsets) -> dict:
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
    spread = {}
    for h in horizons:
        b, a = _mean_excess(buy_meas, h), _mean_excess(avoid_meas, h)
        spread[h] = (b - a) if (b is not None and a is not None) else None
    return {
        "n_buy": len(buys),
        "n_avoid": len(avoids),
        "n_buy_priced": sum(1 for m in buys if m["entry_idx"] is not None),
        "stats": _bucket_stats(buy_meas, horizons),
        "spread": spread,
        "curve": _equity_curve(buys, bench, offsets),
    }


def _normalize_personas(personas) -> list[str]:
    """Validated subset in canonical council order; empty/unknown -> full council."""
    if not personas:
        return list(COUNCIL)
    wanted = set(personas)
    subset = [p for p in COUNCIL if p in wanted]
    return subset or list(COUNCIL)


def run_persona_study(session, benchmark: str = DEFAULT_BENCHMARK,
                      horizons=DEFAULT_HORIZONS, personas=None) -> dict:
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
            "verdicts": verdicts.get(hist.stock_id, {}),
            "series": series,
            "entry_idx": entry_idx,
            "meas": meas,
        })

    per_persona = [
        {"persona": slug, **_portfolio(members, [slug], bench, horizons, offsets)}
        for slug in COUNCIL
    ]

    return {
        "benchmark": benchmark,
        "benchmark_bars": len(bench),
        "horizons": list(horizons),
        "curve_offsets": offsets,
        "cohort_size": len(members),
        "council": list(COUNCIL),
        "personas": per_persona,
        "subset": {"personas": subset, **_portfolio(members, subset, bench, horizons, offsets)},
    }
