"""
The gap-fill closer: read the latest audit's `missing` lists and dispatch to the
existing ingest entrypoints to fill each hole. Writes no source logic of its own.

- identity : copy sector/industry from the stock's already-stored yfinance info into
             Stock columns (DB-only, no network) — the biggest, cheapest win.
- prices   : backfill_prices(symbols=...)   (OHLCV backfill, creates no snapshots)
- screener : enrich(symbols=...)            (sequential — screener rate-limits)
- yfinance : refresh(symbols=...)           (re-fetch minimal/missing snapshots)

Run `dq_audit` first so `data_quality_reports` reflects reality; gapfill targets
exactly what that audit flagged.
"""

from sqlalchemy import select

from db.models import DataQualityReport, Stock
from engine.ingest.screener_enrich import enrich
from engine.ingest.yf_refresh import backfill_prices, refresh
from engine.repo import job_run, latest_snapshot, universe_contains

ALL_TARGETS = ("identity", "prices", "screener", "yfinance")


def _latest_reports(session, symbols, universe):
    sq = (
        select(DataQualityReport.stock_id, DataQualityReport.missing)
        .order_by(DataQualityReport.stock_id, DataQualityReport.checked_at.desc(), DataQualityReport.id.desc())
        .distinct(DataQualityReport.stock_id)
        .subquery()
    )
    q = select(Stock, sq.c.missing).join(sq, sq.c.stock_id == Stock.id)
    if symbols:
        q = q.where(Stock.symbol.in_([s.upper() for s in symbols]))
    if universe:
        q = q.where(universe_contains(universe))
    return session.execute(q).all()


def _fill_identity(session, stocks) -> int:
    """Populate Stock.sector/industry from the already-stored yfinance info."""
    filled = 0
    for st in stocks:
        yf = latest_snapshot(session, st.id, source="yfinance")
        info = (yf.info if yf else None) or {}
        changed = False
        if not st.sector and info.get("sector"):
            st.sector = info["sector"]
            changed = True
        if not st.industry and info.get("industry"):
            st.industry = info["industry"]
            changed = True
        if changed:
            filled += 1
    session.commit()
    return filled


def gapfill(targets=ALL_TARGETS, symbols=None, universe=None, limit=0, delay=None, verbose=True) -> dict:
    """delay: seconds between network requests. None uses each ingest job's default;
    set high (e.g. 6-8s) to stay under screener.in rate limits."""
    targets = tuple(targets)
    net_kw = {"delay": delay} if delay is not None else {}
    with job_run("dq_fill", target=",".join(targets)) as (session, stats):
        reports = _latest_reports(session, symbols, universe)
        need_identity, need_prices, need_screener, need_yf = [], [], [], []
        for st, missing in reports:
            missing = missing or []
            if any(m.startswith("identity") for m in missing):
                need_identity.append(st)
            if "prices" in missing:
                need_prices.append(st.symbol)
            # "latest_quarter" -> re-pull both sources to capture the newest quarter.
            if "screener" in missing or "latest_quarter" in missing:
                need_screener.append(st.symbol)
            if "yfinance" in missing or "yfinance_refresh" in missing or "latest_quarter" in missing:
                need_yf.append(st.symbol)

        def cap(xs):
            return xs[:limit] if limit else xs

        result = {}
        # 1) identity first — cheap, no network, in this session.
        if "identity" in targets:
            result["identity_filled"] = _fill_identity(session, cap(need_identity))

        # 2) network jobs — each opens its own job_run/session (nested observability).
        if "yfinance" in targets and need_yf:
            result["yfinance"] = refresh(symbols=cap(need_yf), verbose=verbose, **net_kw)
        if "screener" in targets and need_screener:
            result["screener"] = enrich(symbols=cap(need_screener), verbose=verbose, **net_kw)
        if "prices" in targets and need_prices:
            result["prices"] = backfill_prices(symbols=cap(need_prices), verbose=verbose, **net_kw)

        stats.update({
            "candidates": {
                "identity": len(need_identity), "prices": len(need_prices),
                "screener": len(need_screener), "yfinance": len(need_yf),
            },
            "targets": list(targets),
            "result": result,
        })
        if verbose:
            print(f"[dq_fill] targets={targets} candidates={stats['candidates']}")
    return stats
