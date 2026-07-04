"""
Discover new stocks from screener.in and register any we don't already track.

Reuses the IPO-list scraper. New symbols are inserted as Stock rows (status="new",
tagged with the target universe) so a later fetch+analyze pass picks them up. This
is the "add any new stocks" half of the living loop.
"""

import time

from sqlalchemy import select

from db.models import Stock
from engine.repo import add_universe_tag, get_or_create_stock, job_run
from src.fetch_ipo_list import build_final_entry, resolve_symbols, scrape_page

PAGE_DELAY = 1.5


def discover_ipos(year=None, pages=5, resolve=True, verbose=True):
    """
    Scrape recent IPOs and register any new symbols.
    Returns stats including the list of newly discovered symbols.
    """
    tag = f"ipo_{year}" if year else "ipo_recent"
    with job_run("discover_ipos", target=tag) as (session, stats):
        known = set(session.scalars(select(Stock.symbol)).all())

        raw = []
        for page in range(1, pages + 1):
            rows = scrape_page(page)
            if not rows:
                break
            if year:
                rows = [r for r in rows if r.get("listing_date", "").startswith(str(year))]
            raw.extend(rows)
            if page < pages:
                time.sleep(PAGE_DELAY)

        if resolve:
            resolve_symbols(raw)

        entries = [build_final_entry(r) for r in raw if r.get("id_type") == "symbol" or r.get("bse_code")]

        new_symbols = []
        seen = set()
        for e in entries:
            symbol = e["symbol"]
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            is_new = symbol not in known
            stock = get_or_create_stock(
                session, symbol,
                company_name=e.get("company_name"),
                nse_symbol=e.get("nse_symbol"),
                yf_symbol=e.get("yf_symbol"),
                listing_date=e.get("listing_date"),
                issue_price=e.get("issue_price"),
                ipo_mcap_cr=e.get("ipo_mcap_cr"),
                screener_url=e.get("screener_url"),
                exchange="NSE" if (e.get("yf_symbol") or "").endswith(".NS") else "BSE",
            )
            add_universe_tag(session, stock, tag)
            if is_new:
                stock.status = "new"
                new_symbols.append(symbol)
                if verbose:
                    print(f"  NEW: {symbol} ({e.get('company_name')}) listed {e.get('listing_date')}")

        session.commit()
        stats.update({
            "scraped": len(entries),
            "already_known": len(entries) - len(new_symbols),
            "new": len(new_symbols),
            "new_symbols": new_symbols[:100],
        })
        if verbose:
            print(f"[discover] scraped {len(entries)} IPOs, {len(new_symbols)} new")
    return stats
