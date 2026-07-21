"""
Refresh existing stocks from yfinance into new snapshots.

Hash-gated: a fetch that produces identical data to the latest snapshot is a
no-op (no duplicate row) — this is the "never store the same data twice" rule.
Reuses the proven serializers from the original fetcher.
"""

import os
import random
import re
import time

import yfinance as yf
from sqlalchemy import func, select

from db.models import DailyPrice, Stock
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


def _float(env, default):
    try:
        return float(os.environ.get(env, default))
    except (TypeError, ValueError):
        return default


REFRESH_DELAY = 2.0
PARK_THRESHOLD = 3  # consecutive failures before an active stock is parked as "stale"
OUTAGE_RATIO = 0.5  # soft-fail fraction above which the batch is a source outage
OUTAGE_MIN_BATCH = 3  # below this, "everything failed" is just a targeted retry failing

# --- throttle handling -----------------------------------------------------
# yfinance rate-limits a rapid burst: a batch fired at a fixed cadence returns
# 76-78% errors. On each fetch error we back off exponentially with jitter so
# the burst slows and the provider recovers; a success resets the pace to base.
BACKOFF_CAP = _float("YF_BACKOFF_CAP", 60.0)      # seconds — ceiling on any single wait
BACKOFF_JITTER = _float("YF_BACKOFF_JITTER", 0.5)  # fraction of the delay added as jitter
# In-run circuit breaker: once enough of a batch has errored, further requests
# only feed the throttle and record garbage, so we stop and let the outage
# breaker below classify the run (which suppresses parking). The threshold meets
# OUTAGE_RATIO, so a tripped circuit is always seen as an outage — the two
# breakers interlock: circuit stops the burst, outage spares the stocks.
CIRCUIT_MIN_ATTEMPTS = int(_float("YF_CIRCUIT_MIN_ATTEMPTS", 8))
CIRCUIT_RATIO = _float("YF_CIRCUIT_RATIO", OUTAGE_RATIO)


def next_delay(base, consecutive_failures, cap=BACKOFF_CAP, jitter=BACKOFF_JITTER,
               rng=random):
    """Seconds to wait before the next fetch. Base pace while healthy; on a run of
    failures, exponential backoff (base·2ⁿ, capped) plus up to `jitter`·delay of
    random jitter so concurrent clients don't re-fire in lockstep."""
    if consecutive_failures <= 0:
        return base
    delay = min(base * (2 ** consecutive_failures), cap)
    return delay + rng.uniform(0, delay * jitter)


class _Pacer:
    """Adaptive inter-request pacing: base delay while fetches succeed, exponential
    backoff (with jitter) across a run of failures, reset on the next success."""

    def __init__(self, base, cap=BACKOFF_CAP, jitter=BACKOFF_JITTER, rng=random):
        self.base, self.cap, self.jitter, self.rng = base, cap, jitter, rng
        self.fails = 0

    def record(self, ok: bool):
        self.fails = 0 if ok else self.fails + 1

    def sleep(self):
        time.sleep(next_delay(self.base, self.fails, self.cap, self.jitter, self.rng))


def _tripped(errors: int, processed: int) -> bool:
    """True once enough of the batch has errored to call it a throttle/outage."""
    return processed >= CIRCUIT_MIN_ATTEMPTS and errors / processed > CIRCUIT_RATIO


def _history_rows(yf_symbol):
    """Fetch one symbol's full OHLCV history → (rows, errored). `errored` is True
    only when the provider call RAISED (throttle/network) — an empty-but-successful
    response is ([], False), so genuine no-data names don't drive the backoff."""
    try:
        hist = yf.Ticker(yf_symbol).history(period="max")
    except Exception as e:
        from src.utils import log
        log(f"    {yf_symbol}: history fetch failed - {e}")
        return [], True
    return _price_rows(hist), False


def names_agree(a: str | None, b: str | None) -> bool:
    """Loose company-name agreement, used to block promotion when yfinance's
    longName clearly names a *different* listed company than the screener slug
    resolved to. Provider and exchange names differ in suffixes and punctuation,
    so agreement is judged on the first significant token only.

    Deliberately asymmetric with gapfill's `_names_agree`: that one fills
    identity data (ISIN) only on *clear agreement* and is stricter (both leading
    tokens must match); this one blocks promotion only on *clear disagreement*
    and is looser (first token suffices). Both fail-safe in their own direction —
    gapfill withholds on doubt, this admits on doubt.
    """
    if not a or not b:
        return True  # cannot disprove; do not block on missing data
    drop = {"ltd", "limited", "the", "india", "industries", "company", "co", "pvt", "private"}

    def toks(s):
        parts = [t for t in re.sub(r"[^a-z0-9 ]", " ", s.lower()).split() if t not in drop]
        return parts[:2]

    ta, tb = toks(a), toks(b)
    return bool(ta) and bool(tb) and ta[0] == tb[0]


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
    # Guard identity before anything is stored: if the fetched company's name
    # clearly disagrees with ours, the slug resolved to a different listed
    # business. Return early so no snapshot (and no daily prices) is written —
    # a mismatched payload must never overwrite this stock's stored data.
    if not names_agree((payload.get("info") or {}).get("longName"), stock.company_name):
        return "identity_mismatch"
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
                    limit=0, delay=1.0, oldest_first=False, verbose=True):
    """Populate daily_prices for a set of stocks WITHOUT creating snapshots.

    Decoupled from the snapshot/analysis pipeline: it fetches only yfinance
    history, inserting new OHLCV bars and correcting any the provider has since
    rebased. Safe to run alongside analysis — it never changes a stock's latest
    snapshot hash, so it can't retrigger staleness.

    `oldest_first` orders the batch by how stale each stock's newest bar is
    (never-priced stocks first, then the oldest bar). A chunked daily pass with
    this ordering self-heals: whatever the previous run couldn't reach floats to
    the front of the next, so no active name lags the rotation for long.
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
        if oldest_first:
            newest_bar = (
                select(DailyPrice.stock_id,
                       func.max(DailyPrice.date).label("last_bar"))
                .group_by(DailyPrice.stock_id)
                .subquery()
            )
            q = (q.outerjoin(newest_bar, Stock.id == newest_bar.c.stock_id)
                  .order_by(newest_bar.c.last_bar.asc().nulls_first(), Stock.symbol))
        else:
            q = q.order_by(Stock.symbol)
        stocks = session.scalars(q).all()
        if limit:
            stocks = stocks[:limit]

        counts = {"stocks": 0, "bars_added": 0, "bars_rebased": 0, "no_symbol": 0,
                  "error": 0, "no_history": 0}
        pacer = _Pacer(base=delay)
        processed = 0
        for i, stock in enumerate(stocks):
            if not stock.yf_symbol:
                counts["no_symbol"] += 1
                continue
            rows, errored = _history_rows(stock.yf_symbol)
            processed += 1
            pacer.record(ok=not errored)
            if errored:
                counts["error"] += 1
            elif not rows:
                counts["no_history"] += 1
            else:
                counts["bars_added"] += upsert_daily_prices(session, stock.id, rows, counts)
            counts["stocks"] += 1
            session.commit()
            if verbose:
                print(f"  [{i+1}/{len(stocks)}] {stock.symbol}: +{len(rows)} bars")

            # Same throttle circuit breaker as refresh: stop the burst once the
            # provider is clearly rate-limiting, rather than draining the chunk
            # into a wall of errors. The next run's oldest-first ordering picks
            # up exactly where this one bailed.
            if _tripped(counts["error"], processed):
                counts["circuit_broken"] = True
                if verbose:
                    print(f"  circuit broken: {counts['error']}/{processed} errored — "
                          f"stopping to let yfinance recover")
                break

            if i < len(stocks) - 1:
                pacer.sleep()

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
                  "soft_fail": 0, "bars_rebased": 0, "identity_mismatch": 0}
        soft_failed: list[Stock] = []
        pacer = _Pacer(base=delay)
        processed = 0
        fetch_errors = 0
        for i, stock in enumerate(stocks):
            was_new = stock.status == "new"
            res = refresh_one(session, stock, counts)
            key = "error" if res.startswith("error") else res
            counts[key] = counts.get(key, 0) + 1
            processed += 1
            errored = res.startswith("error")
            if errored:
                fetch_errors += 1
            # Pace off provider errors only: a clean fetch (even a no_symbol skip)
            # keeps us at base; a throttle error stretches the next wait.
            pacer.record(ok=not errored)

            if res == "identity_mismatch":
                # A wrong-company payload: don't promote, don't park, don't bump
                # failures (this isn't a transient fetch blip). Notify once so the
                # slug/ticker mapping gets a human look; nothing was stored.
                from engine.notify import notify_safe
                notify_safe("stock identity mismatch (wrong company fetched)",
                            f"{stock.symbol} ({stock.company_name}) resolved to a "
                            f"different listed company on yfinance — the slug/ticker "
                            f"mapping is wrong. Payload discarded, not stored.",
                            tags="warning")
                if verbose:
                    print(f"  [{i+1}/{len(stocks)}] {stock.symbol}: identity_mismatch")
                session.commit()
                if i < len(stocks) - 1:
                    pacer.sleep()
                continue

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
                # Bump/park decisions are DEFERRED to batch end: only there can we
                # tell one dead stock from a source outage, and an outage must not
                # penalise stocks at all (else three outage nights park the universe).
                counts["soft_fail"] += 1
                soft_failed.append(stock)

            if verbose:
                tag = "new" if was_new else stock.status
                print(f"  [{i+1}/{len(stocks)}] {stock.symbol} ({tag}): {res}")
            session.commit()

            # In-run circuit breaker: once the error rate says "throttle", stop —
            # further requests only deepen the throttle and record garbage. The
            # outage breaker below then sees a mostly-failed run and spares the
            # stocks (threshold meets OUTAGE_RATIO, so this interlock always holds).
            if _tripped(fetch_errors, processed):
                counts["circuit_broken"] = True
                if verbose:
                    print(f"  circuit broken: {fetch_errors}/{processed} errored — "
                          f"stopping to let yfinance recover")
                break

            if i < len(stocks) - 1:
                pacer.sleep()

        # Source-outage circuit breaker: when most of the batch soft-failed the
        # source is down, not the stocks. Suppress every bump and park (no counter
        # penalty — the stocks did nothing wrong) and page once, not per stock.
        # Denominator is what we actually attempted (`processed`), not the planned
        # batch — a circuit break stops early, and diluting by unfetched stocks
        # would wrongly clear the outage and park the throttled names.
        if processed >= OUTAGE_MIN_BATCH and len(soft_failed) / processed > OUTAGE_RATIO:
            counts["source_outage"] = True
            from engine.notify import notify_safe
            notify_safe("yfinance outage — parking suppressed",
                        f"{len(soft_failed)} of {processed} stocks soft-failed in one "
                        f"refresh batch — treating this as a source outage. No failure "
                        f"counters bumped, no stocks parked. Re-run once yfinance recovers.",
                        tags="warning")
        else:
            for stock in soft_failed:
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
            session.commit()

        stats.update({"processed": processed, **counts})
    return stats
