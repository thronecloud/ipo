"""
Backfill the ISIN of BSE-only stocks whose `stocks.isin` is NULL.

Why: the bhavcopy ingester resolves BSE rows to our universe by ISIN. A BSE-only
name whose stored `isin` is NULL is therefore unreachable by that match, so it
never receives a price bar — the "active stock, zero bars" tail. These names were
registered by their numeric BSE scrip code (e.g. 544611), which is exactly the
`FinInstrmId` column of the BSE bhavcopy, and that same row carries the ISIN.

So the mapping source is the BSE bhavcopy file ITSELF: scrip code -> ISIN, read
straight from the authoritative EOD file (a few recent trading days unioned, since
illiquid SME scrips do not trade every day). The AMFI categorization file is a
fallback for any scrip the bhavcopy window missed.

SAFETY — wrong ISIN = wrong company's prices, the worst bug class. So:
  - a scrip code that maps to MORE THAN ONE distinct ISIN across the fetched files
    is ambiguous and is REFUSED, never guessed (ISIN revisions on splits do this);
  - the script is DRY-RUN by default: it PRINTS the proposed UPDATEs and a summary,
    and touches nothing. `--execute` is required to write, and even then it writes
    only the unambiguous proposals.

Usage:
    python -m scripts.backfill_bse_isins                 # dry-run against $DATABASE_URL
    python -m scripts.backfill_bse_isins --days 6        # union 6 recent bhavcopy days
    python -m scripts.backfill_bse_isins --execute       # apply the proposed UPDATEs
"""

from __future__ import annotations

import argparse
import csv
import io
from collections import namedtuple
from datetime import date, timedelta

from sqlalchemy import select

from db.base import SessionLocal
from db.models import DailyPrice, Stock
from engine.ingest.amfi import SOURCES_DIR, parse_amfi
from engine.ingest.bhavcopy import _fetch_bse_csv
from engine.ingest.exchange_base import clean

Proposal = namedtuple("Proposal", "symbol isin source label")


# ---------- mapping sources (pure over their inputs) ----------

def parse_bse_scrip_isin(csv_text: str) -> tuple[dict[str, str], dict[str, str]]:
    """A BSE UDiFF bhavcopy CSV -> (scrip_code -> ISIN, TckrSymb -> ISIN).

    Equity rows only. Both maps drop any key seen with two different ISINs in this
    file (ambiguous), by mapping it to None — the caller treats None as "refuse"."""
    scrip: dict[str, str | None] = {}
    tsym: dict[str, str | None] = {}

    def _put(m, key, isin):
        if not key or not isin:
            return
        if key in m and m[key] not in (None, isin):
            m[key] = None                     # conflicting ISIN → poison the key
        elif key not in m:
            m[key] = isin

    for row in csv.DictReader(io.StringIO(csv_text)):
        if clean(row.get("FinInstrmTp")) != "STK":
            continue
        isin = clean(row.get("ISIN"))
        _put(scrip, clean(row.get("FinInstrmId")), isin)
        _put(tsym, (clean(row.get("TckrSymb")) or "").upper() or None, isin)
    return scrip, tsym


def merge_bhavcopy_maps(csv_texts: list[str]) -> tuple[dict[str, str], dict[str, str], set[str]]:
    """Union several bhavcopy days into scrip/tsym ISIN maps, tracking every key that
    ever mapped to two different ISINs (across ALL days) as a conflict to refuse."""
    scrip: dict[str, str] = {}
    tsym: dict[str, str] = {}
    conflicts: set[str] = set()

    def _merge(dst, src):
        for key, isin in src.items():
            if isin is None:                  # already ambiguous within a day
                conflicts.add(key)
                dst.pop(key, None)
                continue
            if key in conflicts:
                continue
            if key in dst and dst[key] != isin:
                conflicts.add(key)
                dst.pop(key, None)
            else:
                dst[key] = isin

    for text in csv_texts:
        s, t = parse_bse_scrip_isin(text)
        _merge(scrip, s)
        _merge(tsym, t)
    return scrip, tsym, conflicts


def amfi_scrip_map(path: str) -> dict[str, str]:
    """BSE scrip code -> ISIN from an AMFI categorization xlsx (fallback source)."""
    out: dict[str, str] = {}
    for rec in parse_amfi(path):
        code, isin = rec.get("bse_symbol"), rec.get("isin")
        if code and isin:
            out.setdefault(str(code).strip(), isin)
    return out


# ---------- proposal logic (pure) ----------

def propose_mappings(targets: list[tuple[str, str | None]],
                     scrip_map: dict[str, str], tsym_map: dict[str, str],
                     amfi_map: dict[str, str], conflicts: set[str],
                     ) -> tuple[list[Proposal], list[str], list[str]]:
    """Resolve each target (symbol, company_name) to a single ISIN.

    A numeric symbol IS a BSE scrip code, matched against the bhavcopy scrip map
    then AMFI; a non-numeric symbol is matched against the bhavcopy TckrSymb map.
    Returns (proposals, ambiguous_symbols, unmatched_symbols)."""
    proposals: list[Proposal] = []
    ambiguous: list[str] = []
    unmatched: list[str] = []
    for symbol, label in targets:
        key = symbol.strip()
        if key.isdigit():
            if key in conflicts:
                ambiguous.append(symbol)
                continue
            if key in scrip_map:
                proposals.append(Proposal(symbol, scrip_map[key], "bhavcopy", label))
            elif key in amfi_map:
                proposals.append(Proposal(symbol, amfi_map[key], "amfi", label))
            else:
                unmatched.append(symbol)
        else:
            up = key.upper()
            if up in conflicts:
                ambiguous.append(symbol)
            elif up in tsym_map:
                proposals.append(Proposal(symbol, tsym_map[up], "bhavcopy_ticker", label))
            else:
                unmatched.append(symbol)
    return proposals, ambiguous, unmatched


# ---------- I/O seams ----------

def load_targets(session) -> list[tuple[str, str | None]]:
    """Active/new stocks with a NULL isin and no price bars — the unreachable tail."""
    priced = select(DailyPrice.stock_id).distinct()
    rows = session.execute(
        select(Stock.symbol, Stock.company_name)
        .where(Stock.status.in_(("active", "new")),
               Stock.isin.is_(None),
               Stock.id.not_in(priced))
        .order_by(Stock.symbol)
    ).all()
    return [(sym, name) for sym, name in rows]


def fetch_recent_bhavcopy(days: int) -> list[str]:
    """CSV text for up to `days` recent BSE trading days (most recent first). Days
    with no published file (weekend/holiday/404) are skipped, not counted."""
    texts: list[str] = []
    d = date.today()
    tried = 0
    while len(texts) < days and tried < days + 8:
        tried += 1
        if d.weekday() < 5:
            text = _fetch_bse_csv(d)
            if text:
                texts.append(text)
        d -= timedelta(days=1)
    return texts


def latest_amfi_file() -> str | None:
    import glob
    import os
    files = glob.glob(os.path.join(SOURCES_DIR, "*.xlsx"))
    return max(files, key=os.path.getmtime) if files else None


# ---------- entrypoint ----------

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=6,
                    help="recent BSE bhavcopy trading days to union (default 6)")
    ap.add_argument("--execute", action="store_true",
                    help="APPLY the proposed UPDATEs (default: dry-run, print only)")
    args = ap.parse_args()

    print(f">>> fetching up to {args.days} recent BSE bhavcopy days …")
    texts = fetch_recent_bhavcopy(args.days)
    print(f"    got {len(texts)} bhavcopy file(s)")
    scrip_map, tsym_map, conflicts = merge_bhavcopy_maps(texts)
    print(f"    scrip→ISIN pairs: {len(scrip_map)}  |  ticker→ISIN pairs: {len(tsym_map)}"
          f"  |  ambiguous keys refused: {len(conflicts)}")

    amfi_path = latest_amfi_file()
    amfi_map = amfi_scrip_map(amfi_path) if amfi_path else {}
    print(f"    AMFI fallback: {len(amfi_map)} scrip→ISIN pairs"
          f" (from {amfi_path or 'none'})")

    session = SessionLocal()
    try:
        targets = load_targets(session)
        print(f"\n>>> targets (active/new, NULL isin, zero bars): {len(targets)}")
        proposals, ambiguous, unmatched = propose_mappings(
            targets, scrip_map, tsym_map, amfi_map, conflicts)

        by_source: dict[str, int] = {}
        print("\n--- proposed UPDATEs (dry-run) ---" if not args.execute
              else "\n--- applying UPDATEs ---")
        for p in proposals:
            by_source[p.source] = by_source.get(p.source, 0) + 1
            print(f"UPDATE stocks SET isin='{p.isin}', isin_source='{p.source}' "
                  f"WHERE symbol='{p.symbol}';   -- {p.label or ''}")

        if args.execute and proposals:
            for p in proposals:
                stock = session.scalar(select(Stock).where(Stock.symbol == p.symbol))
                # Never clobber a value that appeared since load; only fill a NULL.
                if stock is not None and stock.isin is None:
                    stock.isin = p.isin
                    stock.isin_source = p.source
            session.commit()
            print(f"\n>>> COMMITTED {len(proposals)} ISIN backfills.")

        print("\n=== SUMMARY ===")
        print(f"targets                : {len(targets)}")
        print(f"proposed mappings      : {len(proposals)}  {by_source}")
        print(f"ambiguous (refused)    : {len(ambiguous)}  {ambiguous}")
        print(f"unmatched              : {len(unmatched)}  {unmatched}")
        print(f"mode                   : {'EXECUTE (written)' if args.execute else 'DRY-RUN (no writes)'}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
