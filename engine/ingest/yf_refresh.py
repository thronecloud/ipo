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
from engine.repo import (
    add_snapshot,
    bump_fetch_failures,
    extract_columns,
    job_run,
    latest_snapshot,
    reset_fetch_failures,
    universe_contains,
    upsert_daily_prices,
)
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


def _price_rows(history):
    """Full daily OHLCV series from a yfinance history frame → list of bar dicts.
    NaN/inf are coerced to None. Returns [] when there is no usable history."""
    if history is None or getattr(history, "empty", True):
        return []
    if not all(col in history.columns for col in ("Open", "High", "Low", "Close", "Volume")):
        return []
    idx = history.index
    o, h, l, c, v = (history[col] for col in ("Open", "High", "Low", "Close", "Volume"))
    rows = []
    for i in range(len(history)):
        vol = serialize_value(v.iloc[i])
        rows.append({
            "date": idx[i].date(),
            "open": serialize_value(o.iloc[i]),
            "high": serialize_value(h.iloc[i]),
            "low": serialize_value(l.iloc[i]),
            "close": serialize_value(c.iloc[i]),
            "volume": int(vol) if vol is not None else None,
        })
    return rows


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
        # Full OHLCV series rides along under a private key; refresh_one pops it
        # (into daily_prices) BEFORE the payload is hashed/stored, so snapshot
        # dedup semantics are unchanged and snapshots never carry the bulky series.
        "_price_rows": _price_rows(history),
    }
    return payload, _quality(info, financials, balance_sheet, cashflow, history)


def refresh_one(session, stock: Stock, counts=None) -> str:
    if not stock.yf_symbol:
        return "no_symbol"
    # Record the attempt regardless of outcome — freshness tracks attempts, not just
    # hash-changing snapshots (a stable stock refreshed daily stays "fresh").
    from db.models import utcnow
    stock.last_fetched_at = utcnow()
    payload, quality = fetch_payload(stock.yf_symbol)
    if payload is None:
        return quality  # "error: ..."
    # Pop the OHLCV series out before hashing/storing the snapshot payload.
    price_rows = payload.pop("_price_rows", [])
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
    # Idempotent whether or not the snapshot itself was new.
    upsert_daily_prices(session, stock.id, price_rows, counts)
    return "new_snapshot" if created else "unchanged"


def backfill_prices(universe=None, symbols=None, statuses=("active", "new"),
                    limit=0, delay=1.0, verbose=True):
    """Populate daily_prices for a set of stocks WITHOUT creating snapshots.

    Decoupled from the snapshot/analysis pipeline: it fetches only yfinance
    history, inserting new OHLCV bars and correcting any the provider has since
    rebased. Safe to run alongside analysis — it never changes a stock's latest
    snapshot hash, so it can't retrigger staleness.
    """
    _t = universe or (symbols and f"{len(symbols)} symbols") or "all"
    with job_run("backfill_prices", target=_t) as (session, stats):
        q = select(Stock)
        if symbols:
            q = q.where(Stock.symbol.in_(symbols))
        else:
            q = q.where(Stock.status.in_(list(statuses)))
        if universe:
            q = q.where(universe_contains(universe))
        stocks = session.scalars(q.order_by(Stock.symbol)).all()
        if limit:
            stocks = stocks[:limit]

        counts = {"stocks": 0, "bars_added": 0, "bars_rebased": 0, "no_symbol": 0,
                  "error": 0, "no_history": 0}
        for i, stock in enumerate(stocks):
            if not stock.yf_symbol:
                counts["no_symbol"] += 1
                continue
            hist = safe_fetch(
                lambda: yf.Ticker(stock.yf_symbol).history(period="max"),
                "history", stock.yf_symbol,
            )
            rows = _price_rows(hist)
            if not rows:
                counts["no_history"] += 1
            else:
                counts["bars_added"] += upsert_daily_prices(session, stock.id, rows, counts)
            counts["stocks"] += 1
            session.commit()
            if verbose:
                print(f"  [{i+1}/{len(stocks)}] {stock.symbol}: +{len(rows)} bars")
            if i < len(stocks) - 1:
                time.sleep(delay)

        stats.update(counts)
    return stats


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
    _t = universe or (symbols and f"{len(symbols)} symbols") or ",".join(statuses)
    with job_run("refresh", target=_t) as (session, stats):
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
                  "promoted": 0, "unfetchable": 0, "parked": 0, "revived": 0,
                  "soft_fail": 0, "bars_rebased": 0}
        for i, stock in enumerate(stocks):
            was_new = stock.status == "new"
            res = refresh_one(session, stock, counts)
            key = "error" if res.startswith("error") else res
            counts[key] = counts.get(key, 0) + 1

            got_snapshot = res in ("new_snapshot", "unchanged")
            hard_fail = res == "no_symbol"
            # "Viable" = we actually obtained analysis-grade (full) data. A minimal/limited
            # snapshot is NOT viable — it must not promote a `new` stock nor reset failures
            # (that was the "degraded data logged as success, never parked" bug).
            latest = latest_snapshot(session, stock.id, source="yfinance") if got_snapshot else None
            viable = latest is not None and latest.data_quality == "full"

            if viable:
                reset_fetch_failures(session, stock)
                if promote and was_new:
                    stock.status = "active"
                    counts["promoted"] += 1
                elif promote and stock.status in ("stale", "unfetchable"):
                    stock.status = "active"          # a parked stock became fetchable → revive
                    counts["revived"] += 1
            elif hard_fail:
                if promote and was_new:
                    stock.status = "unfetchable"     # no yfinance symbol = genuinely unfetchable
                    counts["unfetchable"] += 1
            else:
                # Soft failure: a transient error OR a below-viability (minimal) snapshot.
                # Give grace via the failure counter; park only after PARK_THRESHOLD — a
                # single blip no longer exiles a freshly-discovered stock forever.
                counts["soft_fail"] += 1
                n = bump_fetch_failures(session, stock)
                if promote and n >= PARK_THRESHOLD:
                    if stock.status == "new":
                        stock.status = "unfetchable"
                        counts["unfetchable"] += 1
                    elif stock.status in ("active", "stale"):
                        stock.status = "stale"
                        counts["parked"] += 1
                        from engine.notify import notify_safe
                        notify_safe("stock auto-parked (repeated fetch failures)",
                                    f"{stock.symbol} ({stock.company_name}) failed "
                                    f"{n} consecutive fetches — possible delisting or symbol "
                                    f"change. Retry sweep runs weekly; or "
                                    f"engine.run refresh --status stale --symbols {stock.symbol}",
                                    tags="package")

            if verbose:
                tag = "new" if was_new else stock.status
                print(f"  [{i+1}/{len(stocks)}] {stock.symbol} ({tag}): {res}")
            session.commit()
            if i < len(stocks) - 1:
                time.sleep(delay)

        stats.update({"processed": len(stocks), **counts})
    return stats
