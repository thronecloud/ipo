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

# HTTP statuses that mean the host is throttling/banning us rather than that a
# single page is missing. scrape_company_page swallows the response into a string
# error (requests.HTTPError stringifies as "<status> <reason> Error: … for url"),
# so the status survives only as the leading token of that message.
_RATE_LIMIT_STATUSES = ("429", "403")


class RateLimited(Exception):
    """screener.in returned 429/403 — stop the batch, don't deepen the ban."""


def _looks_rate_limited(err: str | None) -> bool:
    return bool(err) and err.split(" ", 1)[0] in _RATE_LIMIT_STATUSES


def _fin_score(data: dict) -> int:
    return sum(len(data.get(k, {})) for k in ("profit_loss", "quarterly_results",
                                              "balance_sheet", "cash_flow"))


def scrape_best(symbol: str, screener_url: str | None = None):
    """Try consolidated then standalone; keep whichever has the richest statements.

    Raises RateLimited on a 429/403 — once the host is throttling us every further
    URL returns the same, so there is nothing to gain by trying the rest.
    """
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
            if _looks_rate_limited(err):
                raise RateLimited(err)
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
    _t = universe or (symbols and f"{len(symbols)} symbols") or "all"
    with job_run("screener_enrich", target=_t) as (session, stats):
        q = select(Stock).where(Stock.status == "active")
        if symbols:
            q = q.where(Stock.symbol.in_(symbols))
        if universe:
            q = q.where(universe_contains(universe))
        stocks = session.scalars(q.order_by(Stock.symbol)).all()
        if limit:
            stocks = stocks[:limit]

        counts = Counter()
        processed = 0
        for i, stock in enumerate(stocks):
            try:
                res = enrich_one(session, stock)
            except RateLimited as e:
                # Circuit breaker: the host is banning us. Every remaining stock
                # would 429 too, returning no_data across the batch and recording
                # a false "success". Abort now, flag it, and page once.
                from engine.notify import notify_safe
                notify_safe("screener enrichment rate-limited (batch aborted)",
                            f"screener.in returned '{e}' — aborted after {processed} "
                            f"of {len(stocks)} stocks to stop hammering a rate-limiting "
                            f"host. Re-run once the ban clears.", tags="warning")
                stats["rate_limited"] = True
                if verbose:
                    print(f"  [{i+1}/{len(stocks)}] {stock.symbol}: rate_limited — aborting batch")
                break
            except Exception as e:
                res = "error"
                if verbose:
                    print(f"      {stock.symbol}: {e}")
            counts[res] += 1
            processed += 1
            if verbose:
                print(f"  [{i+1}/{len(stocks)}] {stock.symbol}: {res}")
            session.commit()
            if i < len(stocks) - 1:
                time.sleep(delay)

        stats.update({"processed": processed, **dict(counts)})
    return stats
