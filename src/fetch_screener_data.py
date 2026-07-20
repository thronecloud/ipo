"""
Stage 2b: Fetch detailed financial data from screener.in company pages.

Supplements yfinance data — especially for stocks where yfinance has
minimal/partial coverage. Scrapes: key ratios, P&L, balance sheet,
cash flow, shareholding from individual company pages.

Updates existing data/stocks/{SYMBOL}.json files with screener data.
"""

import argparse
import re
import time

import requests
from bs4 import BeautifulSoup

from src.utils import log, save_json, load_json, file_exists

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; IPOAnalyzer/1.0)"}
PAGE_DELAY = 1.5  # seconds between requests


def parse_number(text):
    """Parse Indian number format: '1,35,942' or '30.7' or '-2' to float."""
    if not text:
        return None
    cleaned = text.replace(",", "").replace("%", "").strip()
    if not cleaned or cleaned == "—" or cleaned == "-":
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def extract_classification(soup):
    """Sector→industry chain from the peers section, broadest to most specific.

    Screener's peers header links the full market classification
    (e.g. Consumer Discretionary > Textiles > ... > Other Textile Products).
    This is the ONLY sector source for fresh listings — yfinance carries
    nothing for them and AMFI only classifies established names.
    """
    peers = soup.find("section", id="peers")
    sub = peers.find("p", class_="sub") if peers else None
    if not sub:
        return []
    return [a.get_text(strip=True) for a in sub.find_all("a")
            if a.get_text(strip=True)]


def scrape_top_ratios(soup):
    """Extract key ratios from the top section of the company page."""
    ratios = {}
    lis = soup.find_all("li")
    for li in lis:
        name_el = li.find("span", class_="name")
        val_el = li.find("span", class_="number") or li.find("span", class_="value")
        if name_el and val_el:
            name = name_el.get_text(strip=True)
            val = val_el.get_text(strip=True)
            ratios[name] = val
    return ratios


def scrape_table(table):
    """Parse an HTML table into a list of lists."""
    rows = []
    for tr in table.find_all("tr"):
        cells = []
        for td in tr.find_all(["th", "td"]):
            cells.append(td.get_text(strip=True))
        if cells:
            rows.append(cells)
    return rows


def table_to_dict(rows):
    """
    Convert table rows into a dict of {metric: {period: value}}.
    First row is headers (periods), subsequent rows are metric data.
    """
    if not rows or len(rows) < 2:
        return {}

    headers = rows[0]
    result = {}
    for row in rows[1:]:
        if not row:
            continue
        metric = row[0].rstrip("+").strip()
        if not metric:
            continue
        values = {}
        for i, val in enumerate(row[1:], 1):
            if i < len(headers):
                period = headers[i]
                parsed = parse_number(val)
                if parsed is not None:
                    values[period] = parsed
        if values:
            result[metric] = values
    return result


def scrape_company_page(url):
    """Scrape a screener.in company page for all financial data."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        return None, str(e)

    soup = BeautifulSoup(resp.text, "html.parser")

    data = {
        "screener_url": url,
        "ratios": {},
        "profit_loss": {},
        "quarterly_results": {},
        "balance_sheet": {},
        "cash_flow": {},
        "shareholding": {},
        "about": "",
    }

    # 1. Top ratios
    data["ratios"] = scrape_top_ratios(soup)
    data["classification"] = extract_classification(soup)

    # 2. About/description
    about_section = soup.find("div", class_="about")
    if about_section:
        p = about_section.find("p")
        if p:
            data["about"] = p.get_text(strip=True)

    # 3. Parse all tables by section
    tables = soup.find_all("table")

    # Table 0: Quarterly results
    if len(tables) > 0:
        rows = scrape_table(tables[0])
        data["quarterly_results"] = table_to_dict(rows)

    # Table 1: Annual P&L
    if len(tables) > 1:
        rows = scrape_table(tables[1])
        data["profit_loss"] = table_to_dict(rows)

    # Find balance sheet and cash flow by section ID
    bs_section = soup.find("section", id="balance-sheet")
    if bs_section:
        bs_table = bs_section.find("table")
        if bs_table:
            rows = scrape_table(bs_table)
            data["balance_sheet"] = table_to_dict(rows)

    cf_section = soup.find("section", id="cash-flow")
    if cf_section:
        cf_table = cf_section.find("table")
        if cf_table:
            rows = scrape_table(cf_table)
            data["cash_flow"] = table_to_dict(rows)

    # Shareholding pattern
    sh_section = soup.find("section", id="shareholding")
    if sh_section:
        sh_table = sh_section.find("table")
        if sh_table:
            rows = scrape_table(sh_table)
            data["shareholding"] = table_to_dict(rows)

    return data, None


def merge_screener_into_stock(stock_path, screener_data):
    """Merge screener.in data into an existing stock JSON file."""
    stock = load_json(stock_path) or {}

    # Add screener data as a new top-level key
    stock["screener"] = screener_data

    # Upgrade data quality if screener fills gaps
    has_pl = bool(screener_data.get("profit_loss"))
    has_bs = bool(screener_data.get("balance_sheet"))
    has_cf = bool(screener_data.get("cash_flow"))
    has_ratios = bool(screener_data.get("ratios"))

    # Enrich the info dict with screener ratios if yfinance is missing them
    info = stock.get("info", {})
    ratios = screener_data.get("ratios", {})

    if not info.get("currentPrice") and ratios.get("Current Price"):
        info["currentPrice"] = parse_number(ratios["Current Price"])

    if not info.get("trailingPE") and ratios.get("Stock P/E"):
        info["trailingPE"] = parse_number(ratios["Stock P/E"])

    if not info.get("returnOnEquity") and ratios.get("ROE"):
        roe = parse_number(ratios["ROE"])
        if roe is not None:
            info["returnOnEquity"] = roe / 100.0

    if not info.get("bookValue") and ratios.get("Book Value"):
        info["bookValue"] = parse_number(ratios["Book Value"])

    if not info.get("marketCap") and ratios.get("Market Cap"):
        mcap = parse_number(ratios["Market Cap"])
        if mcap is not None:
            info["marketCap"] = mcap * 1e7  # Convert Cr to absolute

    if not info.get("dividendYield") and ratios.get("Dividend Yield"):
        dy = parse_number(ratios["Dividend Yield"])
        if dy is not None:
            # Percentage points, matching yfinance's own dividendYield scale.
            # returnOnEquity above divides by 100 because yfinance stores THAT
            # one as a fraction; the two are not the same convention.
            info["dividendYield"] = dy

    # Add ROCE (not in yfinance)
    if ratios.get("ROCE"):
        info["roce"] = parse_number(ratios["ROCE"])

    # Add about/description if missing
    if not info.get("longBusinessSummary") and screener_data.get("about"):
        info["longBusinessSummary"] = screener_data["about"]

    stock["info"] = info

    # Update data quality
    old_quality = stock.get("data_quality", "minimal")
    screener_score = sum([has_pl, has_bs, has_cf, has_ratios])
    yf_score = {"full": 4, "partial": 2, "limited": 1, "minimal": 0, "none": 0}.get(old_quality, 0)
    combined = max(yf_score, screener_score)

    if combined >= 3:
        stock["data_quality"] = "full"
    elif combined >= 2:
        stock["data_quality"] = "partial"
    elif combined >= 1:
        stock["data_quality"] = "limited"
    else:
        stock["data_quality"] = "minimal"

    save_json(stock_path, stock)
    return stock["data_quality"]


def main():
    parser = argparse.ArgumentParser(description="Fetch financial data from screener.in company pages")
    parser.add_argument("--input", default="data/ipo_list.json", help="IPO list JSON")
    parser.add_argument("--force", action="store_true", help="Re-fetch even if screener data exists")
    parser.add_argument("--only-incomplete", action="store_true", help="Only fetch for stocks with partial/minimal yfinance data")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of stocks")
    parser.add_argument("--symbol", type=str, help="Fetch a single stock")
    args = parser.parse_args()

    ipo_list = load_json(args.input)
    if not ipo_list:
        log(f"ERROR: Cannot load {args.input}")
        return

    stocks = ipo_list["stocks"]

    if args.symbol:
        stocks = [s for s in stocks if s["symbol"] == args.symbol]
        if not stocks:
            log(f"ERROR: Symbol {args.symbol} not found")
            return

    if args.only_incomplete:
        incomplete = []
        for s in stocks:
            stock_data = load_json(f"data/stocks/{s['symbol']}.json")
            if stock_data and stock_data.get("data_quality") in ("minimal", "partial"):
                incomplete.append(s)
        stocks = incomplete
        log(f"Filtered to {len(stocks)} incomplete stocks")

    if args.limit:
        stocks = stocks[:args.limit]

    log(f"Fetching screener.in data for {len(stocks)} stocks")

    counts = {"success": 0, "skipped": 0, "error": 0}
    quality_upgrades = {"full": 0, "partial": 0, "limited": 0, "minimal": 0}

    for i, stock in enumerate(stocks):
        symbol = stock["symbol"]
        stock_path = f"data/stocks/{symbol}.json"
        screener_url = stock.get("screener_url", "")

        if not screener_url:
            log(f"  [{i+1}/{len(stocks)}] {symbol}: No screener URL, skipping")
            counts["error"] += 1
            continue

        # Check if already has screener data
        existing = load_json(stock_path)
        if existing and existing.get("screener") and not args.force:
            counts["skipped"] += 1
            continue

        screener_data, error = scrape_company_page(screener_url)

        if error:
            log(f"  [{i+1}/{len(stocks)}] {symbol}: ERROR - {error}")
            counts["error"] += 1
        elif screener_data:
            new_quality = merge_screener_into_stock(stock_path, screener_data)
            counts["success"] += 1
            quality_upgrades[new_quality] = quality_upgrades.get(new_quality, 0) + 1

            has_data = []
            if screener_data.get("profit_loss"): has_data.append("P&L")
            if screener_data.get("balance_sheet"): has_data.append("BS")
            if screener_data.get("cash_flow"): has_data.append("CF")
            if screener_data.get("ratios"): has_data.append("Ratios")

            log(f"  [{i+1}/{len(stocks)}] {symbol}: {', '.join(has_data)} -> quality={new_quality}")
        else:
            counts["error"] += 1

        if i < len(stocks) - 1:
            time.sleep(PAGE_DELAY)

        if (i + 1) % 50 == 0:
            log(f"  Progress: {i+1}/{len(stocks)}")

    log("=" * 50)
    log(f"Screener fetch complete: {counts['success']} success, {counts['skipped']} skipped, {counts['error']} errors")
    log(f"Quality after merge: {quality_upgrades}")


if __name__ == "__main__":
    main()
