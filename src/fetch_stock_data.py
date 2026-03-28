"""
Stage 2: Fetch stock data from yfinance for each IPO.

Reads data/ipo_list.json, fetches .info, .financials, .balance_sheet,
.cashflow, .history for each stock, saves to data/stocks/{SYMBOL}.json.

Idempotent: skips stocks whose JSON already exists (unless --force).
"""

import argparse
import math
import time
from datetime import datetime

import numpy as np
import pandas as pd
import yfinance as yf

from src.utils import log, save_json, load_json, file_exists, ensure_dir

OUTPUT_DIR = "data/stocks"
BATCH_DELAY = 2.0  # seconds between individual stock fetches


def serialize_value(val):
    """Convert a single value to JSON-safe type."""
    if val is None:
        return None
    if isinstance(val, (np.integer,)):
        return int(val)
    if isinstance(val, (np.floating,)):
        if math.isnan(val) or math.isinf(val):
            return None
        return float(val)
    if isinstance(val, float):
        if math.isnan(val) or math.isinf(val):
            return None
        return val
    if isinstance(val, (pd.Timestamp, datetime)):
        return val.isoformat()
    if isinstance(val, np.bool_):
        return bool(val)
    return val


def serialize_dataframe(df):
    """Convert a pandas DataFrame to a JSON-serializable dict."""
    if df is None or (hasattr(df, "empty") and df.empty):
        return {}

    result = {}
    for idx in df.index:
        row_key = str(idx)
        row_data = {}
        for col in df.columns:
            col_key = str(col)
            row_data[col_key] = serialize_value(df.loc[idx, col])
        result[row_key] = row_data
    return result


def serialize_info(info):
    """Clean up yfinance .info dict for JSON serialization."""
    if not info:
        return {}

    # Remove large/unnecessary fields
    skip_keys = {"companyOfficers", "executiveTeam", "corporateActions", "maxAge", "messageBoardId"}
    cleaned = {}
    for k, v in info.items():
        if k in skip_keys:
            continue
        cleaned[k] = serialize_value(v)
    return cleaned


def safe_fetch(fn, name, symbol):
    """Wrap yfinance calls that may fail individually."""
    try:
        result = fn()
        if result is not None and not (hasattr(result, "empty") and result.empty):
            return result
        return None
    except Exception as e:
        log(f"    {symbol}: {name} fetch failed - {e}")
        return None


def fetch_single_stock(symbol, yf_symbol, ipo_data, force=False):
    """Fetch all data for a single stock. Returns status string."""
    output_path = f"{OUTPUT_DIR}/{symbol}.json"

    if file_exists(output_path) and not force:
        return "skipped"

    try:
        ticker = yf.Ticker(yf_symbol)
        info = ticker.info

        # Check for valid data - info should have meaningful content
        if not info or len(info) < 15:
            # Very limited data - still save what we have
            log(f"    {symbol}: Limited data ({len(info or {})} fields)")

        # Fetch each data type independently
        financials = safe_fetch(lambda: ticker.financials, "financials", symbol)
        balance_sheet = safe_fetch(lambda: ticker.balance_sheet, "balance_sheet", symbol)
        cashflow = safe_fetch(lambda: ticker.cashflow, "cashflow", symbol)
        history = safe_fetch(lambda: ticker.history(period="max"), "history", symbol)

        # Build the stock data object
        stock_data = {
            "symbol": symbol,
            "yf_symbol": yf_symbol,
            "fetched_at": datetime.utcnow().isoformat() + "Z",
            "fetch_status": "success",
            "info": serialize_info(info),
            "financials": serialize_dataframe(financials),
            "balance_sheet": serialize_dataframe(balance_sheet),
            "cashflow": serialize_dataframe(cashflow),
            "history_summary": {},
        }

        # Summarize history (don't store all daily prices - too large)
        if history is not None and not history.empty:
            stock_data["history_summary"] = {
                "first_date": str(history.index[0].date()),
                "last_date": str(history.index[-1].date()),
                "total_days": len(history),
                "first_close": serialize_value(history["Close"].iloc[0]),
                "last_close": serialize_value(history["Close"].iloc[-1]),
                "high_52w": serialize_value(history["Close"].tail(252).max()) if len(history) >= 252 else serialize_value(history["Close"].max()),
                "low_52w": serialize_value(history["Close"].tail(252).min()) if len(history) >= 252 else serialize_value(history["Close"].min()),
                "avg_volume_30d": serialize_value(history["Volume"].tail(30).mean()) if len(history) >= 30 else serialize_value(history["Volume"].mean()),
            }

        # Add IPO-specific data from ipo_list
        stock_data["ipo_data"] = {
            "listing_date": ipo_data.get("listing_date"),
            "issue_price": ipo_data.get("issue_price"),
            "ipo_mcap_cr": ipo_data.get("ipo_mcap_cr"),
            "company_name": ipo_data.get("company_name"),
            "screener_url": ipo_data.get("screener_url"),
        }

        # Compute data quality
        has_info = len(info or {}) > 30
        has_financials = financials is not None
        has_bs = balance_sheet is not None
        has_cf = cashflow is not None
        has_history = history is not None and not history.empty

        quality_score = sum([has_info, has_financials, has_bs, has_cf, has_history])
        if quality_score >= 4:
            stock_data["data_quality"] = "full"
        elif quality_score >= 2:
            stock_data["data_quality"] = "partial"
        elif quality_score >= 1:
            stock_data["data_quality"] = "limited"
        else:
            stock_data["data_quality"] = "minimal"

        save_json(output_path, stock_data)
        return "success"

    except Exception as e:
        log(f"    {symbol}: FAILED - {e}")
        # Save error stub so we don't retry on next run
        save_json(output_path, {
            "symbol": symbol,
            "yf_symbol": yf_symbol,
            "fetched_at": datetime.utcnow().isoformat() + "Z",
            "fetch_status": "error",
            "error": str(e),
            "data_quality": "none",
        })
        return "error"


def main():
    parser = argparse.ArgumentParser(description="Fetch stock data from yfinance")
    parser.add_argument("--input", default="data/ipo_list.json", help="IPO list JSON")
    parser.add_argument("--force", action="store_true", help="Re-fetch existing stocks")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of stocks to fetch (0=all)")
    parser.add_argument("--symbol", type=str, help="Fetch a single stock by symbol")
    args = parser.parse_args()

    ipo_list = load_json(args.input)
    if not ipo_list:
        log(f"ERROR: Cannot load {args.input}")
        return

    stocks = ipo_list["stocks"]
    if args.symbol:
        stocks = [s for s in stocks if s["symbol"] == args.symbol]
        if not stocks:
            log(f"ERROR: Symbol {args.symbol} not found in IPO list")
            return

    if args.limit:
        stocks = stocks[:args.limit]

    ensure_dir(OUTPUT_DIR)

    log(f"Fetching stock data for {len(stocks)} stocks")

    counts = {"success": 0, "skipped": 0, "error": 0}

    for i, stock in enumerate(stocks):
        symbol = stock["symbol"]
        yf_symbol = stock["yf_symbol"]

        if not yf_symbol:
            log(f"  [{i+1}/{len(stocks)}] {symbol}: No yfinance symbol, skipping")
            counts["error"] += 1
            continue

        status = fetch_single_stock(symbol, yf_symbol, stock, force=args.force)
        counts[status] += 1

        if status != "skipped":
            log(f"  [{i+1}/{len(stocks)}] {symbol}: {status}")

        if status != "skipped" and i < len(stocks) - 1:
            time.sleep(BATCH_DELAY)

        if (i + 1) % 50 == 0:
            log(f"  Progress: {i+1}/{len(stocks)} (success={counts['success']}, skipped={counts['skipped']}, errors={counts['error']})")

    log("=" * 50)
    log(f"Fetch complete: {counts['success']} success, {counts['skipped']} skipped, {counts['error']} errors")


if __name__ == "__main__":
    main()
