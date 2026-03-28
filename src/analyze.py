"""
Stage 3: Run AI investor persona analysis for each stock.

For each stock × persona combination, invokes `claude -p` with the
persona's system prompt and the stock's financial data. Results are
saved to data/analyses/{SYMBOL}/{persona_slug}.json.

Idempotent: skips analyses whose JSON already exists (unless --force).
Uses Claude Max plan via CLI — no API key needed.
"""

import argparse
import json
import subprocess
import time
from datetime import datetime

from src.utils import log, save_json, load_json, file_exists, ensure_dir
from src.personas import (
    PERSONAS,
    ANALYSIS_JSON_SCHEMA,
    ANALYSIS_PROMPT_TEMPLATE,
    get_persona,
    get_persona_slugs,
)

ANALYSES_DIR = "data/analyses"
STOCKS_DIR = "data/stocks"
CALL_DELAY = 2.0  # seconds between Claude CLI calls
MAX_RETRIES = 3
RETRY_DELAY = 10  # seconds between retries


def format_currency(value):
    """Format large numbers for display: 1234567890 -> '1,234.6 Cr'."""
    if value is None:
        return "N/A"
    if isinstance(value, str):
        return value
    if value >= 1e9:
        return f"{value/1e7:,.0f} Cr"
    if value >= 1e7:
        return f"{value/1e7:,.1f} Cr"
    if value >= 1e5:
        return f"{value/1e5:,.1f} L"
    return f"{value:,.0f}"


def format_pct(value):
    """Format percentage: 0.152 -> '15.2%'."""
    if value is None:
        return "N/A"
    if isinstance(value, str):
        return value
    return f"{value * 100:.1f}%"


def build_financial_summary(stock_data):
    """Build a concise financial summary string from stock data for the analysis prompt."""
    info = stock_data.get("info", {})
    lines = []

    # Valuation
    section = []
    for key, label in [
        ("trailingPE", "P/E (TTM)"),
        ("forwardPE", "P/E (Forward)"),
        ("priceToBook", "Price/Book"),
        ("priceToSalesTrailing12Months", "Price/Sales"),
        ("enterpriseValue", "Enterprise Value"),
        ("enterpriseToEbitda", "EV/EBITDA"),
        ("enterpriseToRevenue", "EV/Revenue"),
    ]:
        val = info.get(key)
        if val is not None:
            if key == "enterpriseValue":
                section.append(f"{label}: {format_currency(val)}")
            else:
                section.append(f"{label}: {val:.2f}" if isinstance(val, float) else f"{label}: {val}")
    if section:
        lines.append("VALUATION:")
        lines.extend(f"  {s}" for s in section)

    # Profitability
    section = []
    for key, label in [
        ("returnOnEquity", "ROE"),
        ("operatingMargins", "Operating Margin"),
        ("profitMargins", "Net Margin"),
        ("grossMargins", "Gross Margin"),
        ("ebitdaMargins", "EBITDA Margin"),
    ]:
        val = info.get(key)
        if val is not None:
            section.append(f"{label}: {format_pct(val)}")
    if section:
        lines.append("\nPROFITABILITY:")
        lines.extend(f"  {s}" for s in section)

    # Growth
    section = []
    for key, label in [
        ("revenueGrowth", "Revenue Growth (YoY)"),
        ("earningsGrowth", "Earnings Growth (YoY)"),
    ]:
        val = info.get(key)
        if val is not None:
            section.append(f"{label}: {format_pct(val)}")
    if section:
        lines.append("\nGROWTH:")
        lines.extend(f"  {s}" for s in section)

    # Balance Sheet
    section = []
    for key, label, fmt in [
        ("debtToEquity", "Debt/Equity", lambda v: f"{v:.1f}"),
        ("currentRatio", "Current Ratio", lambda v: f"{v:.2f}"),
        ("totalDebt", "Total Debt", format_currency),
        ("totalCash", "Total Cash", format_currency),
        ("freeCashflow", "Free Cash Flow", format_currency),
        ("operatingCashflow", "Operating Cash Flow", format_currency),
        ("totalRevenue", "Total Revenue", format_currency),
        ("ebitda", "EBITDA", format_currency),
        ("netIncomeToCommon", "Net Income", format_currency),
        ("bookValue", "Book Value/Share", lambda v: f"INR {v:.2f}"),
    ]:
        val = info.get(key)
        if val is not None:
            section.append(f"{label}: {fmt(val)}")
    if section:
        lines.append("\nBALANCE SHEET & CASH FLOW:")
        lines.extend(f"  {s}" for s in section)

    # Share data
    section = []
    for key, label, fmt in [
        ("heldPercentInsiders", "Insider Holding", format_pct),
        ("heldPercentInstitutions", "Institutional Holding", format_pct),
        ("dividendYield", "Dividend Yield", format_pct),
    ]:
        val = info.get(key)
        if val is not None:
            section.append(f"{label}: {fmt(val)}")
    if section:
        lines.append("\nSHAREHOLDING:")
        lines.extend(f"  {s}" for s in section)

    # Income statement highlights (from financials)
    financials = stock_data.get("financials", {})
    if financials:
        lines.append("\nINCOME STATEMENT (available years):")
        key_metrics = ["Total Revenue", "Net Income", "EBITDA"]
        for metric in key_metrics:
            if metric in financials:
                row = financials[metric]
                vals = [f"{k[:10]}: {format_currency(v)}" for k, v in row.items() if v is not None]
                if vals:
                    lines.append(f"  {metric}: {' | '.join(vals)}")

    # Business description
    desc = info.get("longBusinessSummary", "")
    if desc:
        if len(desc) > 200:
            desc = desc[:197] + "..."
        lines.append(f"\nBUSINESS: {desc}")

    if not lines:
        return "LIMITED DATA AVAILABLE — This is a recent IPO with minimal public financial history."

    return "\n".join(lines)


def run_claude_analysis(system_prompt, user_prompt, model="opus"):
    """
    Run Claude CLI for a single analysis.
    Returns (structured_output_dict, metadata_dict) on success, (None, error_str) on failure.
    """
    schema_str = json.dumps(ANALYSIS_JSON_SCHEMA)

    cmd = [
        "claude", "-p",
        "--no-session-persistence",
        "--model", model,
        "--tools", "",
        "--system-prompt", system_prompt,
        "--output-format", "json",
        "--json-schema", schema_str,
        user_prompt,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            start_new_session=True,
        )

        if result.returncode != 0:
            return None, f"CLI exit code {result.returncode}: {result.stderr[:200]}"

        if not result.stdout.strip():
            return None, "Empty stdout from CLI"

        response = json.loads(result.stdout)

        if response.get("is_error"):
            return None, f"Claude error: {response.get('result', 'unknown')}"

        structured = response.get("structured_output")
        if not structured:
            return None, "No structured_output in response"

        metadata = {
            "duration_ms": response.get("duration_ms"),
            "model_used": model,
            "total_cost_usd": response.get("total_cost_usd"),
            "usage": response.get("usage", {}),
        }

        return structured, metadata

    except subprocess.TimeoutExpired:
        return None, "CLI timeout (120s)"
    except json.JSONDecodeError as e:
        return None, f"JSON parse error: {e}"
    except Exception as e:
        return None, f"Unexpected error: {e}"


def analyze_single(symbol, stock_data, persona, model="opus", force=False):
    """Run analysis for one stock × one persona. Returns status string."""
    slug = persona["slug"]
    output_path = f"{ANALYSES_DIR}/{symbol}/{slug}.json"

    if file_exists(output_path) and not force:
        return "skipped"

    # Skip stocks without complete data
    if stock_data.get("fetch_status") == "error":
        return "no_data"
    if stock_data.get("data_quality") not in ("full",):
        return "no_data"

    info = stock_data.get("info", {})
    ipo = stock_data.get("ipo_data", {})

    # Build the financial summary
    financial_summary = build_financial_summary(stock_data)

    # Build the user prompt from template
    current_price = info.get("currentPrice") or info.get("regularMarketPrice", "N/A")
    market_cap = info.get("marketCap")

    user_prompt = ANALYSIS_PROMPT_TEMPLATE.format(
        company_name=info.get("longName") or ipo.get("company_name", symbol),
        symbol=symbol,
        exchange="NSE" if stock_data.get("yf_symbol", "").endswith(".NS") else "BSE",
        sector=info.get("sector", "Unknown"),
        industry=info.get("industry", "Unknown"),
        listing_date=ipo.get("listing_date", "Unknown"),
        issue_price=ipo.get("issue_price", "N/A"),
        current_price=current_price,
        market_cap_display=format_currency(market_cap),
        ipo_return_pct=f"{ipo.get('ipo_return_pct', 'N/A')}%" if ipo.get("ipo_return_pct") is not None else "N/A",
        financial_summary=financial_summary,
    )

    # Run with retries
    for attempt in range(MAX_RETRIES):
        analysis, meta_or_error = run_claude_analysis(
            system_prompt=persona["system_prompt"],
            user_prompt=user_prompt,
            model=model,
        )

        if analysis is not None:
            # Success — save result
            result = {
                "symbol": symbol,
                "persona": slug,
                "persona_display_name": persona["display_name"],
                "analyzed_at": datetime.utcnow().isoformat() + "Z",
                "analysis": analysis,
                "metadata": meta_or_error,
            }
            ensure_dir(f"{ANALYSES_DIR}/{symbol}")
            save_json(output_path, result)
            return "success"

        # Failed
        error_msg = meta_or_error
        if attempt < MAX_RETRIES - 1:
            log(f"      Attempt {attempt + 1} failed: {error_msg}. Retrying in {RETRY_DELAY}s...")
            time.sleep(RETRY_DELAY)
        else:
            log(f"      All {MAX_RETRIES} attempts failed: {error_msg}")
            return "error"

    return "error"


def main():
    parser = argparse.ArgumentParser(description="Run AI investor persona analysis")
    parser.add_argument("--symbol", type=str, help="Analyze a single stock")
    parser.add_argument("--persona", type=str, help="Use a single persona (slug)")
    parser.add_argument("--model", default="opus", help="Claude model to use (default: opus)")
    parser.add_argument("--force", action="store_true", help="Re-analyze existing results")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of stocks (0=all)")
    parser.add_argument("--delay", type=float, default=CALL_DELAY, help="Delay between calls (seconds)")
    args = parser.parse_args()

    # Load IPO list
    ipo_list = load_json("data/ipo_list.json")
    if not ipo_list:
        log("ERROR: Cannot load data/ipo_list.json")
        return

    stocks = ipo_list["stocks"]

    # Filter by symbol if specified
    if args.symbol:
        stocks = [s for s in stocks if s["symbol"] == args.symbol]
        if not stocks:
            log(f"ERROR: Symbol {args.symbol} not found in IPO list")
            return

    if args.limit:
        stocks = stocks[:args.limit]

    # Get personas
    if args.persona:
        persona = get_persona(args.persona)
        if not persona:
            log(f"ERROR: Persona '{args.persona}' not found. Available: {get_persona_slugs()}")
            return
        personas = [persona]
    else:
        personas = list(PERSONAS.values())

    total_combos = len(stocks) * len(personas)
    log(f"Analyzing {len(stocks)} stocks x {len(personas)} personas = {total_combos} analyses")
    log(f"Model: {args.model}, delay: {args.delay}s")

    counts = {"success": 0, "skipped": 0, "error": 0, "no_data": 0}
    done = 0

    for si, stock_entry in enumerate(stocks):
        symbol = stock_entry["symbol"]

        # Load stock data
        stock_path = f"{STOCKS_DIR}/{symbol}.json"
        stock_data = load_json(stock_path)
        if not stock_data:
            log(f"  [{si+1}/{len(stocks)}] {symbol}: No stock data file, skipping")
            counts["no_data"] += len(personas)
            done += len(personas)
            continue

        for pi, persona in enumerate(personas):
            status = analyze_single(
                symbol, stock_data, persona,
                model=args.model, force=args.force,
            )
            counts[status] += 1
            done += 1

            if status == "success":
                score = ""
                analysis_path = f"{ANALYSES_DIR}/{symbol}/{persona['slug']}.json"
                result = load_json(analysis_path)
                if result:
                    score = f" (score: {result['analysis'].get('score', '?')})"
                log(f"  [{done}/{total_combos}] {symbol} x {persona['display_name']}: {status}{score}")

            if status != "skipped" and done < total_combos:
                time.sleep(args.delay)

        if (si + 1) % 10 == 0:
            log(f"  Progress: {si+1}/{len(stocks)} stocks, {done}/{total_combos} analyses")
            log(f"    Success: {counts['success']}, Skipped: {counts['skipped']}, Errors: {counts['error']}, No data: {counts['no_data']}")

    log("=" * 50)
    log(f"Analysis complete:")
    log(f"  Success: {counts['success']}")
    log(f"  Skipped: {counts['skipped']}")
    log(f"  Errors:  {counts['error']}")
    log(f"  No data: {counts['no_data']}")


if __name__ == "__main__":
    main()
