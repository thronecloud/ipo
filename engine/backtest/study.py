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

from datetime import date

from sqlalchemy import select

from db.models import CompositeScoreHistory, DailyPrice, IndexPrice, Stock

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
        # Entry: first close STRICTLY after the information date (no lookahead).
        entry_idx = next((i for i, (d, _) in enumerate(series) if d > info_date), None)

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
            "entry_date": None, "entry_price": None,
            "returns": {h: None for h in horizons},
            "excess": {h: None for h in horizons},
            "latest_date": None, "latest_price": None, "return_to_date": None,
            "excess_to_date": None,
        }

        if entry_idx is not None:
            entry_d, entry_px = series[entry_idx]
            row["entry_date"], row["entry_price"] = entry_d, entry_px
            last_d, last_px = series[-1]
            row["latest_date"], row["latest_price"] = last_d, last_px
            if entry_px:
                row["return_to_date"] = _pct(entry_px, last_px)

            b_entry = _first_on_or_after(bench, entry_d)
            for h in horizons:
                if entry_idx + h < len(series) and entry_px:
                    exit_d, exit_px = series[entry_idx + h]
                    row["returns"][h] = _pct(entry_px, exit_px)
                    if b_entry:
                        b_exit = _last_on_or_before(bench, exit_d)
                        if b_exit and b_exit[0] > b_entry[0] and b_entry[1]:
                            row["excess"][h] = row["returns"][h] - _pct(b_entry[1], b_exit[1])
                        elif b_exit and b_entry[1]:
                            # Same-bar window (thin benchmark): 0% benchmark move.
                            row["excess"][h] = row["returns"][h]
            if b_entry and b_entry[1] and row["return_to_date"] is not None:
                b_exit = _last_on_or_before(bench, last_d)
                if b_exit:
                    row["excess_to_date"] = row["return_to_date"] - _pct(b_entry[1], b_exit[1])

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
