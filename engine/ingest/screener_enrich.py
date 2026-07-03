"""
Screener.in enrichment — the multi-year / quarterly / ROCE / promoter-holding data
that yfinance lacks for Indian stocks (see scripts/dq_benchmark.py findings).

Stored as a separate `source="screener"` snapshot (hash-gated), combined with the
yfinance snapshot at prompt-build time. Runs at its own cadence (fundamentals move
quarterly, not daily).
"""

import time
from collections import Counter

from sqlalchemy import select

from db.models import Stock
from engine.repo import add_snapshot, job_run, universe_contains
from src.fetch_screener_data import scrape_company_page

DELAY = 1.5


def _fin_score(data: dict) -> int:
    return sum(len(data.get(k, {})) for k in ("profit_loss", "quarterly_results",
                                              "balance_sheet", "cash_flow"))


def scrape_best(symbol: str, screener_url: str | None = None):
    """Try consolidated then standalone; keep whichever has the richest statements."""
    candidates, seen = [], set()
    for u in ([screener_url] if screener_url else []) + [
        f"https://www.screener.in/company/{symbol}/consolidated/",
        f"https://www.screener.in/company/{symbol}/",
    ]:
        if u and u not in seen:
            seen.add(u)
            candidates.append(u)

    best, best_score = None, -1
    for url in candidates:
        data, err = scrape_company_page(url)
        if not data:
            continue
        score = _fin_score(data)
        if score > best_score:
            best, best_score = (data, url), score
        if score > 0:   # got real statements — good enough, stop hitting more URLs
            break
        time.sleep(0.5)
    return best


def screener_quality(data: dict) -> str:
    score = sum(bool(data.get(k)) for k in ("profit_loss", "balance_sheet", "cash_flow", "ratios"))
    return {4: "full", 3: "full", 2: "partial", 1: "limited"}.get(score, "minimal")


def enrich_one(session, stock: Stock) -> str:
    res = scrape_best(stock.symbol, stock.screener_url)
    if not res:
        return "no_data"
    data, url = res
    if not (data.get("profit_loss") or data.get("ratios")):
        return "empty"
    data["screener_url"] = url
    _, created = add_snapshot(
        session, stock, {"screener": data}, {},
        source="screener", fetch_status="success", data_quality=screener_quality(data),
    )
    return "new_snapshot" if created else "unchanged"


def enrich(universe=None, symbols=None, limit=0, delay=DELAY, verbose=True):
    """Scrape screener for a set of stocks into screener snapshots. Returns stats."""
    with job_run("screener_enrich", target=universe or (symbols and ",".join(symbols)) or "all") as (session, stats):
        q = select(Stock).where(Stock.status == "active")
        if symbols:
            q = q.where(Stock.symbol.in_(symbols))
        if universe:
            q = q.where(universe_contains(universe))
        stocks = session.scalars(q.order_by(Stock.symbol)).all()
        if limit:
            stocks = stocks[:limit]

        counts = Counter()
        for i, stock in enumerate(stocks):
            try:
                res = enrich_one(session, stock)
            except Exception as e:
                res = "error"
                if verbose:
                    print(f"      {stock.symbol}: {e}")
            counts[res] += 1
            if verbose:
                print(f"  [{i+1}/{len(stocks)}] {stock.symbol}: {res}")
            session.commit()
            if i < len(stocks) - 1:
                time.sleep(delay)

        stats.update({"processed": len(stocks), **dict(counts)})
    return stats
