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
                      "total_nse_smallcap": new + updated})
        if verbose:
            print(f"[amfi] {release_tag}: {new} new, {updated} updated "
                  f"({new + updated} NSE small-caps total)")
    return stats
