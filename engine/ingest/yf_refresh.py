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
    balance_sheet = safe_fetch(lambda: ticker.balance_sheet, "balance_sheet", yf_symbol)
    cashflow = safe_fetch(lambda: ticker.cashflow, "cashflow", yf_symbol)
    history = safe_fetch(lambda: ticker.history(period="max"), "history", yf_symbol)

    payload = {
        "info": serialize_info(info),
        "financials": serialize_dataframe(financials),
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


def refresh(universe=None, symbols=None, limit=0, delay=REFRESH_DELAY, verbose=True):
    """Refresh a set of stocks. Returns a stats dict."""
    with job_run("refresh", target=universe or (symbols and ",".join(symbols)) or "all") as (session, stats):
        q = select(Stock).where(Stock.status == "active").order_by(Stock.symbol)
        if symbols:
            q = q.where(Stock.symbol.in_(symbols))
        if universe:
            q = q.where(universe_contains(universe))
        stocks = session.scalars(q).all()
        if limit:
            stocks = stocks[:limit]

        counts = {"new_snapshot": 0, "unchanged": 0, "no_symbol": 0, "error": 0}
        for i, stock in enumerate(stocks):
            res = refresh_one(session, stock)
            key = "error" if res.startswith("error") else res
            counts[key] = counts.get(key, 0) + 1
            if verbose:
                print(f"  [{i+1}/{len(stocks)}] {stock.symbol}: {res}")
            session.commit()
            if i < len(stocks) - 1:
                time.sleep(delay)

        stats.update({"processed": len(stocks), **counts})
    return stats
