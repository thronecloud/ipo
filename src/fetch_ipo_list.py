"""
Stage 1: Scrape IPO list from screener.in/ipo/recent/

Fetches all pages, filters for 2025 IPOs, extracts stock symbols from
company link hrefs. For numeric-ID hrefs, visits the company page to
resolve the NSE/BSE symbol.

Output: data/ipo_list.json
"""

import argparse
import re
import time

import requests
from bs4 import BeautifulSoup

from src.utils import log, save_json, load_json, file_exists

BASE_URL = "https://www.screener.in/ipo/recent/"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; IPOAnalyzer/1.0)"}
PAGE_DELAY = 1.5  # seconds between page fetches
RESOLVE_DELAY = 1.0  # seconds between company page fetches


def parse_price(text):
    """Parse price string like '₹146' or '₹1,352.50' to float. Returns None if unparseable."""
    cleaned = text.replace("₹", "").replace(",", "").strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_mcap(text):
    """Parse market cap string like '2,195' or '91,000' to float (in crores). Returns None if unparseable."""
    cleaned = text.replace(",", "").strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_pct_change(text):
    """Parse '⇡ 49%' or '⇣ 16%' or '+11%' to float. Returns None if unparseable."""
    is_negative = "⇣" in text or "down" in text.lower()
    # Remove all non-numeric chars except dot and minus
    cleaned = re.sub(r"[^0-9.\-]", "", text).strip()
    if not cleaned:
        return None
    try:
        value = float(cleaned)
        return -abs(value) if is_negative else value
    except ValueError:
        return None


def parse_listing_date(text):
    """Parse '02 Apr 2026' to 'YYYY-MM-DD' string."""
    from datetime import datetime
    try:
        dt = datetime.strptime(text.strip(), "%d %b %Y")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return text.strip()


def extract_symbol_from_href(href):
    """
    Extract stock symbol from screener.in company href.

    Patterns:
      /company/SWIGGY/              -> ('SWIGGY', 'symbol')
      /company/NTPCGREEN/consolidated/ -> ('NTPCGREEN', 'symbol')
      /company/544291/              -> ('544291', 'bse_code')
      /company/id/1285148/          -> ('1285148', 'screener_id')
    """
    if not href:
        return None, "unknown"

    # Pattern: /company/id/<numeric>/...
    match = re.match(r"/company/id/(\d+)", href)
    if match:
        return match.group(1), "screener_id"

    # Pattern: /company/<something>/...
    match = re.match(r"/company/([^/]+)/?", href)
    if match:
        identifier = match.group(1)
        if identifier.isdigit():
            return identifier, "bse_code"
        else:
            return identifier, "symbol"

    return None, "unknown"


def resolve_symbol_from_company_page(screener_path):
    """
    Visit a screener.in company page to extract NSE and BSE symbols.
    Returns dict with 'nse_symbol' and/or 'bse_code'.
    """
    url = f"https://www.screener.in{screener_path}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        log(f"    Failed to fetch {url}: {e}")
        return {}

    soup = BeautifulSoup(resp.text, "html.parser")
    text = soup.get_text()

    result = {}

    # Find NSE symbol
    nse_match = re.search(r"NSE:\s*(\w+)", text)
    if nse_match:
        result["nse_symbol"] = nse_match.group(1).strip()

    # Find BSE code
    bse_match = re.search(r"BSE(?:\s*-\s*\w+)?:\s*(\d+)", text)
    if bse_match:
        result["bse_code"] = bse_match.group(1).strip()

    return result


def scrape_page(page_num):
    """Scrape a single page of screener.in/ipo/recent/. Returns list of raw IPO dicts."""
    url = BASE_URL if page_num == 1 else f"{BASE_URL}?page={page_num}"
    log(f"  Fetching page {page_num}: {url}")

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        log(f"  ERROR fetching page {page_num}: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table")
    if not table:
        log(f"  No table found on page {page_num}")
        return []

    rows = table.find_all("tr")[1:]  # skip header
    results = []

    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 6:
            continue

        link = cells[0].find("a")
        href = link["href"] if link else ""
        company_name = cells[0].get_text(strip=True)
        listing_date_raw = cells[1].get_text(strip=True)
        ipo_mcap_raw = cells[2].get_text(strip=True)
        ipo_price_raw = cells[3].get_text(strip=True)
        current_price_raw = cells[4].get_text(strip=True)
        pct_change_raw = cells[5].get_text(strip=True)

        identifier, id_type = extract_symbol_from_href(href)

        results.append({
            "company_name": company_name,
            "listing_date": parse_listing_date(listing_date_raw),
            "ipo_mcap_cr": parse_mcap(ipo_mcap_raw),
            "issue_price": parse_price(ipo_price_raw),
            "current_price": parse_price(current_price_raw),
            "ipo_return_pct": parse_pct_change(pct_change_raw),
            "screener_href": href,
            "identifier": identifier,
            "id_type": id_type,
        })

    return results


def resolve_symbols(stocks):
    """
    For stocks where we only have a BSE code or screener ID (not an NSE symbol),
    visit the company page to resolve the actual NSE/BSE identifiers.
    """
    needs_resolution = [s for s in stocks if s["id_type"] != "symbol"]
    if not needs_resolution:
        return

    log(f"  Resolving symbols for {len(needs_resolution)} stocks with numeric IDs...")

    for i, stock in enumerate(needs_resolution):
        if i > 0:
            time.sleep(RESOLVE_DELAY)

        resolved = resolve_symbol_from_company_page(stock["screener_href"])

        if resolved.get("nse_symbol"):
            stock["identifier"] = resolved["nse_symbol"]
            stock["id_type"] = "symbol"
            stock["bse_code"] = resolved.get("bse_code")
            log(f"    {stock['company_name']}: resolved to NSE:{resolved['nse_symbol']}")
        elif resolved.get("bse_code"):
            stock["bse_code"] = resolved["bse_code"]
            log(f"    {stock['company_name']}: BSE-only ({resolved['bse_code']})")
        else:
            log(f"    {stock['company_name']}: could not resolve symbol")

        if (i + 1) % 20 == 0:
            log(f"    Progress: {i + 1}/{len(needs_resolution)} resolved")


def build_final_entry(stock):
    """Convert raw scraped data into final IPO list entry."""
    if stock["id_type"] == "symbol":
        symbol = stock["identifier"]
        nse_symbol = f"{symbol}.NS"
        yf_symbol = nse_symbol
    elif stock.get("bse_code"):
        symbol = stock.get("bse_code", stock["identifier"])
        nse_symbol = None
        yf_symbol = f"{symbol}.BO"
    else:
        symbol = stock["identifier"]
        nse_symbol = None
        yf_symbol = None

    return {
        "symbol": symbol,
        "company_name": stock["company_name"],
        "nse_symbol": nse_symbol,
        "yf_symbol": yf_symbol,
        "listing_date": stock["listing_date"],
        "issue_price": stock["issue_price"],
        "current_price": stock["current_price"],
        "ipo_return_pct": stock["ipo_return_pct"],
        "ipo_mcap_cr": stock["ipo_mcap_cr"],
        "screener_url": f"https://www.screener.in{stock['screener_href']}",
    }


def main():
    parser = argparse.ArgumentParser(description="Scrape IPO list from screener.in")
    parser.add_argument("--pages", type=int, default=25, help="Number of pages to scrape (default: 25)")
    parser.add_argument("--year", type=int, default=2025, help="Filter IPOs by listing year (default: 2025)")
    parser.add_argument("--output", default="data/ipo_list.json", help="Output path")
    parser.add_argument("--force", action="store_true", help="Overwrite existing output")
    parser.add_argument("--skip-resolve", action="store_true", help="Skip resolving numeric IDs to symbols")
    args = parser.parse_args()

    if file_exists(args.output) and not args.force:
        existing = load_json(args.output)
        count = len(existing.get("stocks", []))
        log(f"Output already exists with {count} stocks. Use --force to overwrite.")
        return

    log(f"Scraping screener.in IPO list (pages 1-{args.pages}, year={args.year})")

    all_raw = []
    reached_older = False

    for page in range(1, args.pages + 1):
        rows = scrape_page(page)
        if not rows:
            log(f"  No data on page {page}, stopping")
            break

        # Check if we've gone past our target year
        page_years = set()
        for r in rows:
            if r["listing_date"] and len(r["listing_date"]) >= 4:
                try:
                    year = int(r["listing_date"][:4])
                    page_years.add(year)
                except ValueError:
                    pass

        # Filter for target year
        year_rows = [
            r for r in rows
            if r["listing_date"] and r["listing_date"].startswith(str(args.year))
        ]
        all_raw.extend(year_rows)

        log(f"  Page {page}: {len(rows)} total, {len(year_rows)} from {args.year}")

        # If all entries on this page are older than target year, stop
        if page_years and max(page_years) < args.year:
            log(f"  All entries older than {args.year}, stopping")
            reached_older = True
            break

        if page < args.pages:
            time.sleep(PAGE_DELAY)

    log(f"Scraped {len(all_raw)} IPOs from {args.year}")

    # Resolve numeric IDs to symbols
    if not args.skip_resolve:
        resolve_symbols(all_raw)

    # Build final entries
    stocks = [build_final_entry(s) for s in all_raw]

    # Remove entries with no usable symbol
    valid_stocks = [s for s in stocks if s["yf_symbol"]]
    skipped = len(stocks) - len(valid_stocks)
    if skipped:
        log(f"Skipped {skipped} stocks with no resolvable symbol")

    output = {
        "metadata": {
            "generated_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
            "source": "screener.in/ipo/recent/",
            "year": args.year,
            "pages_scraped": min(args.pages, page),
            "total_count": len(valid_stocks),
        },
        "stocks": valid_stocks,
    }

    save_json(args.output, output)
    log(f"Saved {len(valid_stocks)} IPOs to {args.output}")

    # Summary stats
    with_nse = sum(1 for s in valid_stocks if s["nse_symbol"])
    bse_only = sum(1 for s in valid_stocks if not s["nse_symbol"])
    with_price = sum(1 for s in valid_stocks if s["issue_price"] is not None)
    log(f"  NSE: {with_nse}, BSE-only: {bse_only}, with price data: {with_price}")


if __name__ == "__main__":
    main()
