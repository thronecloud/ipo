"""Benchmark index ingestion: keep index_prices current for the backtest.

Same append-only discipline as stock daily_prices, but keyed by yfinance symbol
(indexes are not stocks — no Stock row, no snapshot, no analysis). The fetcher is
isolated in `_fetch_history_rows` so tests monkeypatch it and nothing here ever
hits the network in CI.
"""

import time

import yfinance as yf

from engine.repo import job_run, upsert_index_prices
from engine.ingest.yf_refresh import _price_rows
from src.fetch_stock_data import safe_fetch

# Benchmarks the engine tracks. Yahoo reality check (2026-07-04): ^CNXSC (Nifty
# Smallcap) serves exactly 1 bar — unusable; BSE-SMLCAP.BO stopped updating
# 2024-05-30 — kept only for pre-2024 reference. ^CRSLDX (Nifty 500) is the
# deepest CURRENT series (2005→today) that still contains the smallcap tail, so
# it is the primary excess-return comparator.
BENCHMARKS = {
    "^CRSLDX": "Nifty 500",                # primary excess-return comparator
    "^NSEI": "Nifty 50",                   # headline reference line
    "BSE-SMLCAP.BO": "S&P BSE SmallCap",   # STALE on Yahoo since 2024-05-30
}

FETCH_DELAY = 1.0


def _fetch_history_rows(symbol: str) -> list[dict]:
    hist = safe_fetch(lambda: yf.Ticker(symbol).history(period="max"), "history", symbol)
    return _price_rows(hist)


def refresh_index_prices(symbols=None, delay=FETCH_DELAY, verbose=True):
    """Fetch full history for each benchmark and append any new bars."""
    targets = list(symbols) if symbols else list(BENCHMARKS)
    with job_run("index_prices", target=",".join(targets)) as (session, stats):
        counts = {"indexes": 0, "bars_added": 0, "no_history": 0}
        for i, symbol in enumerate(targets):
            rows = _fetch_history_rows(symbol)
            if not rows:
                counts["no_history"] += 1
            else:
                counts["bars_added"] += upsert_index_prices(session, symbol, rows)
                counts["indexes"] += 1
            session.commit()
            if verbose:
                print(f"  [{i + 1}/{len(targets)}] {symbol}: +{len(rows)} bars fetched")
            if i < len(targets) - 1:
                time.sleep(delay)
        stats.update(counts)
    return stats
