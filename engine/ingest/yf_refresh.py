"""
Refresh existing stocks from yfinance into new snapshots.

Hash-gated: a fetch that produces identical data to the latest snapshot is a
no-op (no duplicate row) — this is the "never store the same data twice" rule.
Reuses the proven serializers from the original fetcher.
"""

import time

import yfinance as yf
from sqlalchemy import select

from db.models import Stock
from engine.repo import add_snapshot, extract_columns, job_run, universe_contains
from src.fetch_stock_data import safe_fetch, serialize_dataframe, serialize_info, serialize_value

REFRESH_DELAY = 2.0
PARK_THRESHOLD = 3  # consecutive failures before an active stock is parked as "stale"


def _history_summary(history):
    if history is None or history.empty:
        return {}
    return {
        "first_date": str(history.index[0].date()),
        "last_date": str(history.index[-1].date()),
        "total_days": len(history),
        "first_close": serialize_value(history["Close"].iloc[0]),
        "last_close": serialize_value(history["Close"].iloc[-1]),
        "high_52w": serialize_value(history["Close"].tail(252).max()) if len(history) >= 252 else serialize_value(history["Close"].max()),
        "low_52w": serialize_value(history["Close"].tail(252).min()) if len(history) >= 252 else serialize_value(history["Close"].min()),
        "avg_volume_30d": serialize_value(history["Volume"].tail(30).mean()) if len(history) >= 30 else serialize_value(history["Volume"].mean()),
    }


def _quality(info, financials, balance_sheet, cashflow, history):
    has_info = len(info or {}) > 30
    has_history = history is not None and not history.empty
    score = sum([has_info, financials is not None, balance_sheet is not None,
                 cashflow is not None, has_history])
    if score >= 4:
        return "full"
    if score >= 2:
        return "partial"
    if score >= 1:
        return "limited"
    return "minimal"


def fetch_payload(yf_symbol):
    """Fetch a stock from yfinance; return (payload, quality) or (None, 'error: ...')."""
    try:
        ticker = yf.Ticker(yf_symbol)
        info = ticker.info
    except Exception as e:
        return None, f"error: {e}"

    financials = safe_fetch(lambda: ticker.financials, "financials", yf_symbol)
    quarterly = safe_fetch(lambda: ticker.quarterly_financials, "quarterly_financials", yf_symbol)
    balance_sheet = safe_fetch(lambda: ticker.balance_sheet, "balance_sheet", yf_symbol)
    cashflow = safe_fetch(lambda: ticker.cashflow, "cashflow", yf_symbol)
    history = safe_fetch(lambda: ticker.history(period="max"), "history", yf_symbol)

    payload = {
        "info": serialize_info(info),
        "financials": serialize_dataframe(financials),
        "quarterly_financials": serialize_dataframe(quarterly),
        "balance_sheet": serialize_dataframe(balance_sheet),
        "cashflow": serialize_dataframe(cashflow),
        "history_summary": _history_summary(history),
    }
    return payload, _quality(info, financials, balance_sheet, cashflow, history)


def refresh_one(session, stock: Stock) -> str:
    if not stock.yf_symbol:
        return "no_symbol"
    payload, quality = fetch_payload(stock.yf_symbol)
    if payload is None:
        return quality  # "error: ..."
    ipo_data = {
        "listing_date": stock.listing_date,
        "issue_price": stock.issue_price,
        "ipo_mcap_cr": stock.ipo_mcap_cr,
        "company_name": stock.company_name,
        "screener_url": stock.screener_url,
    }
    _, created = add_snapshot(
        session, stock, payload, extract_columns(payload["info"]),
        source="yfinance", fetch_status="success",
        data_quality=quality, ipo_data=ipo_data,
    )
    return "new_snapshot" if created else "unchanged"


def refresh(universe=None, symbols=None, statuses=("active", "new"), promote=True,
            limit=0, delay=REFRESH_DELAY, verbose=True):
    """
    Fetch/refresh a set of stocks into snapshots. Returns a stats dict.

    Closes the discovery loop: a `new` stock's first successful fetch promotes it
    to `active` (so analysis picks it up); a `new` stock that can't be fetched at
    all (bad symbol / no yfinance data — common for BSE-only SME) is parked as
    `unfetchable` so it doesn't clog every future batch. `active` stocks that error
    are left active (transient). New stocks are fetched first.
    """
    with job_run("refresh", target=universe or (symbols and ",".join(symbols)) or ",".join(statuses)) as (session, stats):
        q = select(Stock)
        if symbols:
            q = q.where(Stock.symbol.in_(symbols))
        else:
            q = q.where(Stock.status.in_(list(statuses)))
        if universe:
            q = q.where(universe_contains(universe))
        q = q.order_by((Stock.status != "new"), Stock.symbol)  # new stocks first
        stocks = session.scalars(q).all()
        if limit:
            stocks = stocks[:limit]

        counts = {"new_snapshot": 0, "unchanged": 0, "no_symbol": 0, "error": 0,
                  "promoted": 0, "unfetchable": 0, "parked": 0, "revived": 0}
        for i, stock in enumerate(stocks):
            was_new = stock.status == "new"
            res = refresh_one(session, stock)
            ok = res in ("new_snapshot", "unchanged")
            key = "error" if res.startswith("error") else res
            counts[key] = counts.get(key, 0) + 1

            if was_new and promote:
                if ok:
                    stock.status = "active"
                    counts["promoted"] += 1
                else:  # no_symbol / hard fetch error on first attempt
                    stock.status = "unfetchable"
                    counts["unfetchable"] += 1
            elif ok:
                stock.fetch_failures = 0
                if stock.status == "stale":  # manual retry succeeded — revive
                    stock.status = "active"
                    counts["revived"] += 1
            else:
                # Active stock failing repeatedly = likely delisting/symbol change.
                # Park it so it stops clogging every future batch, and alert.
                stock.fetch_failures = (stock.fetch_failures or 0) + 1
                if stock.status == "active" and stock.fetch_failures >= PARK_THRESHOLD:
                    stock.status = "stale"
                    counts["parked"] += 1
                    try:
                        from engine.notify import notify
                        notify("stock auto-parked (repeated fetch failures)",
                               f"{stock.symbol} ({stock.company_name}) failed "
                               f"{stock.fetch_failures} consecutive fetches — "
                               f"possible delisting or symbol change. "
                               f"Retry: engine.run refresh --status stale --symbols {stock.symbol}",
                               tags="package")
                    except Exception:
                        pass

            if verbose:
                tag = "new" if was_new else stock.status
                print(f"  [{i+1}/{len(stocks)}] {stock.symbol} ({tag}): {res}")
            session.commit()
            if i < len(stocks) - 1:
                time.sleep(delay)

        stats.update({"processed": len(stocks), **counts})
    return stats
