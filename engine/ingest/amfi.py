"""
AMFI market-cap categorization ingestion — the authoritative NSE small-cap roster.

AMFI publishes (Jan & Jul) the SEBI-mandated classification of every listed stock:
Large (top 100) / Mid (101-250) / Small (251+), with NSE Symbol, ISIN, and 6-month
average market cap per exchange. This gives us the *complete* NSE small-cap universe
plus clean symbols — no fuzzy name matching, no NSE anti-bot scraping.

Source page: https://www.amfiindia.com/otherdata/categorisation-of-stocks
Re-run each Jan/Jul with the new file to re-classify as stocks move between caps.
"""

import os

import openpyxl
import requests
from sqlalchemy import select

from db.models import Stock
from engine.repo import add_universe_tag, get_or_create_stock, job_run

SOURCES_DIR = "data/sources"

# Column layout of the AMFI xlsx (header on row 2, data from row 3).
COL = {"srno": 0, "company": 1, "isin": 2, "bse_symbol": 3, "bse_mcap": 4,
       "nse_symbol": 5, "nse_mcap": 6, "msei_symbol": 7, "msei_mcap": 8,
       "avg_mcap": 9, "category": 10}

CATEGORY_MAP = {"large cap": "large", "mid cap": "mid", "small cap": "small"}


def _clean(v):
    if v in (None, "-", ""):
        return None
    return str(v).strip()


AMFI_PAGE = "https://www.amfiindia.com/otherdata/categorisation-of-stocks"

_MONTH_NUM = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
              "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}


def discover_latest_amfi_url() -> tuple[str, str]:
    """Scrape the AMFI page for the newest categorization xlsx.

    Returns (absolute_url, release_tag) where release_tag is e.g. "amfi_2025h2"
    (31Dec2025 file = H2 classification of that year; 30Jun = H1).
    """
    import re

    r = requests.get(AMFI_PAGE, headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
    r.raise_for_status()
    # hrefs come in several forms across releases: absolute (https://...),
    # protocol-relative (//portal.amfiindia.com/...), and site-relative (/Themes/...)
    matches = re.findall(
        r'((?:https?:)?/?/[^"\'\s]*AverageMarketCapitalization(\d{2})(\w{3})(\d{4})\.xlsx)',
        r.text, flags=re.IGNORECASE,
    )
    if not matches:
        raise RuntimeError("no AverageMarketCapitalization*.xlsx links found on AMFI page")

    def sort_key(m):
        _, day, mon, year = m
        return (int(year), _MONTH_NUM.get(mon.lower()[:3], 0), int(day))

    href, _, mon, year = max(matches, key=sort_key)
    if href.startswith("http"):
        url = href
    elif href.startswith("//"):
        url = f"https:{href}"
    elif href.startswith("/") and "." in href.split("/")[1]:
        # "/portal.amfiindia.com/..." — a host path missing its scheme+slash
        url = f"https:/{href}"
    else:
        url = f"https://www.amfiindia.com{href}"
    half = "h1" if _MONTH_NUM.get(mon.lower()[:3], 0) <= 6 else "h2"
    return url, f"amfi_{year}{half}"


def auto_reingest(verbose: bool = True) -> dict:
    """Idempotent scheduled entry point: ingest the latest AMFI release only if
    its release tag isn't in the DB yet. Safe to run monthly."""
    from sqlalchemy import func, select

    from db.base import SessionLocal
    from db.models import Stock
    from engine.repo import universe_contains

    url, tag = discover_latest_amfi_url()
    session = SessionLocal()
    try:
        already = session.scalar(
            select(func.count()).select_from(Stock).where(universe_contains(tag))
        ) or 0
    finally:
        session.close()
    if already > 0:
        if verbose:
            print(f"[amfi] latest release {tag} already ingested ({already} stocks) — skip")
        return {"release": tag, "skipped": True}

    if verbose:
        print(f"[amfi] NEW release detected: {tag} — downloading {url}")
    path = download_amfi(url)
    stats = ingest_smallcaps(path, release_tag=tag, verbose=verbose)
    try:
        from engine.notify import notify
        notify("AMFI reclassification ingested",
               f"{tag}: {stats.get('new', 0)} new small-caps, "
               f"{stats.get('updated', 0)} updated "
               f"({stats.get('total_nse_smallcap', 0)} total)", tags="card_index_dividers")
    except Exception:
        pass
    return stats


def download_amfi(url: str, dest: str | None = None) -> str:
    """Download an AMFI categorization xlsx to data/sources/. Returns the local path."""
    os.makedirs(SOURCES_DIR, exist_ok=True)
    dest = dest or os.path.join(SOURCES_DIR, url.rsplit("/", 1)[-1])
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
    r.raise_for_status()
    with open(dest, "wb") as f:
        f.write(r.content)
    return dest


def parse_amfi(path: str) -> list[dict]:
    """Parse an AMFI xlsx into a list of stock category records."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb["Sheet1"] if "Sheet1" in wb.sheetnames else wb.active
    out = []
    for r in ws.iter_rows(min_row=3, values_only=True):
        if not r or r[COL["company"]] is None:
            continue
        cat_raw = _clean(r[COL["category"]])
        out.append({
            "company": _clean(r[COL["company"]]),
            "isin": _clean(r[COL["isin"]]),
            "nse_symbol": _clean(r[COL["nse_symbol"]]),
            "bse_symbol": _clean(r[COL["bse_symbol"]]),
            "nse_mcap_cr": r[COL["nse_mcap"]] if isinstance(r[COL["nse_mcap"]], (int, float)) else None,
            "category": CATEGORY_MAP.get((cat_raw or "").lower()),
        })
    return out


def ingest_smallcaps(path: str, release_tag: str, nse_only: bool = True, verbose: bool = True):
    """
    Register/refresh the NSE small-cap universe from an AMFI file.
    New stocks get status='new' (picked up by the fetch loop); existing stocks get
    their isin/cap_category/universe tags updated. Returns a stats dict.
    """
    rows = parse_amfi(path)
    with job_run("amfi_smallcap", target=release_tag) as (session, stats):
        known = set(session.scalars(select(Stock.symbol)).all())

        # Reclassification: stocks we already track that are now mid/large in
        # this release get their cap_category updated (graduations out of
        # small-cap). We never create new rows for mid/large names.
        reclassified = 0
        for row in rows:
            if row["category"] in ("mid", "large") and row["nse_symbol"] in known:
                stock = session.scalar(select(Stock).where(Stock.symbol == row["nse_symbol"]))
                if stock and stock.cap_category != row["category"]:
                    if verbose:
                        print(f"  RECLASSIFIED: {stock.symbol} {stock.cap_category} -> {row['category']}")
                    stock.cap_category = row["category"]
                    reclassified += 1

        new = updated = 0
        for row in rows:
            if row["category"] != "small":
                continue
            sym = row["nse_symbol"]
            if nse_only and not sym:
                continue
            if not sym:
                continue

            is_new = sym not in known
            stock = get_or_create_stock(
                session, sym,
                company_name=row["company"],
                yf_symbol=f"{sym}.NS",
                exchange="NSE",
            )
            # Authoritative fields — set/refresh even on existing rows.
            stock.isin = stock.isin or row["isin"]
            stock.cap_category = "small"
            add_universe_tag(stock, "nse_smallcap")
            add_universe_tag(stock, release_tag)

            if is_new:
                stock.status = "new"
                new += 1
                known.add(sym)
                if verbose and new <= 15:
                    print(f"  NEW: {sym} ({row['company']}) ~Rs {round(row['nse_mcap_cr'] or 0)} cr")
            else:
                updated += 1

        session.commit()
        stats.update({"release": release_tag, "new": new, "updated": updated,
                      "reclassified_to_mid_or_large": reclassified,
                      "total_nse_smallcap": new + updated})
        if verbose:
            print(f"[amfi] {release_tag}: {new} new, {updated} updated, "
                  f"{reclassified} reclassified up ({new + updated} NSE small-caps total)")
    return stats
