"""
Corporate announcements — the NSE and BSE corporate-filings feeds, pulled directly.

NSE: https://www.nseindia.com/api/corporate-announcements?index=equities (JSON list,
needs the homepage cookie dance).
BSE: https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w (JSON {"Table": [...]},
needs a bseindia.com Referer).

Both feeds re-serve the same recent window on every poll, so ingestion is idempotent:
each row carries a dedup key of (exchange, source announcement id OR content hash) and
lands via ON CONFLICT DO NOTHING. Symbols are resolved to our universe where they
match; an unmatched announcement is kept with stock_id NULL (it may be a company we do
not track). The `_fetch_*` functions are the only network seam — tests feed recorded
JSON straight to the parsers.
"""

import hashlib
from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert

from db.models import CorporateAnnouncement
from engine.ingest.exchange_base import (
    BSE_BASE,
    NSE_BASE,
    bse_session,
    clean,
    nse_session,
    resolve_stock_id,
)
from engine.repo import job_run

NSE_ANNOUNCEMENTS = f"{NSE_BASE}/api/corporate-announcements?index=equities"
NSE_ANN_REFERER = f"{NSE_BASE}/companies-listing/corporate-filings-announcements"
BSE_ANNOUNCEMENTS = "https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w"
BSE_ATTACH_BASE = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"

# Announcement categories that the report fetcher treats as downloadable results /
# annual reports. Matched case-insensitively as substrings against the category text.
RESULTS_KEYWORDS = ("financial result", "financial results", "results")
ANNUAL_REPORT_KEYWORDS = ("annual report",)


def _parse_nse_dt(v):
    s = clean(v)
    if not s:
        return None
    for fmt in ("%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%b-%Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _parse_bse_dt(v):
    s = clean(v)
    if not s:
        return None
    # BSE serves ISO-ish "2025-01-02T18:30:00" and "2025-01-02 18:30:00.000".
    s = s.replace("T", " ").split(".")[0]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _dedup_hash(exchange: str, announcement_id: str | None, *fallback_parts) -> str:
    """Stable dedup digest. A source-native id makes it exact; otherwise a hash of the
    identifying content (symbol + headline + timestamp + attachment) stands in."""
    if announcement_id:
        basis = f"{exchange}|id|{announcement_id}"
    else:
        basis = f"{exchange}|" + "|".join(str(p or "") for p in fallback_parts)
    return hashlib.sha256(basis.encode()).hexdigest()


def parse_nse_announcements(payload) -> list[dict]:
    """NSE corporate-announcements JSON (a list of dicts) → normalized rows."""
    out = []
    for item in payload or []:
        if not isinstance(item, dict):
            continue
        symbol = clean(item.get("symbol"))
        headline = clean(item.get("attchmntText") or item.get("desc") or item.get("sm_name"))
        category = clean(item.get("desc"))
        if symbol is None and headline is None:
            continue
        attachment = clean(item.get("attchmntFile"))
        announced_at = _parse_nse_dt(item.get("an_dt") or item.get("sort_date"))
        ann_id = clean(item.get("seq_id") or item.get("nseAnnouncementId"))
        out.append({
            "exchange": "NSE",
            "symbol": symbol,
            "headline": headline,
            "category": category,
            "attachment_url": attachment,
            "announced_at": announced_at,
            "announcement_id": ann_id,
            "dedup_hash": _dedup_hash("NSE", ann_id, symbol, headline,
                                      item.get("an_dt"), attachment),
            "raw": item,
        })
    return out


def parse_bse_announcements(payload) -> list[dict]:
    """BSE AnnGetData JSON ({"Table": [...]}) → normalized rows. BSE keys by scrip
    code, so `symbol` is the scrip code string (rarely in our NSE-keyed universe —
    those rows stay stock_id NULL, which is fine)."""
    rows = payload.get("Table") if isinstance(payload, dict) else payload
    out = []
    for item in rows or []:
        if not isinstance(item, dict):
            continue
        symbol = clean(item.get("SCRIP_CD"))
        headline = clean(item.get("NEWSSUB") or item.get("HEADLINE") or item.get("NEWS_SUBJECT"))
        category = clean(item.get("CATEGORYNAME"))
        if symbol is None and headline is None:
            continue
        attach_name = clean(item.get("ATTACHMENTNAME"))
        attachment = f"{BSE_ATTACH_BASE}{attach_name}" if attach_name else None
        announced_at = _parse_bse_dt(item.get("NEWS_DT") or item.get("DissemDT"))
        ann_id = clean(item.get("NEWSID"))
        out.append({
            "exchange": "BSE",
            "symbol": symbol,
            "headline": headline,
            "category": category,
            "attachment_url": attachment,
            "announced_at": announced_at,
            "announcement_id": ann_id,
            "dedup_hash": _dedup_hash("BSE", ann_id, symbol, headline,
                                      item.get("NEWS_DT"), attachment),
            "raw": item,
        })
    return out


def upsert_announcements(session, rows: list[dict], counts: dict | None = None) -> int:
    """Insert normalized announcement rows, skipping any already seen (same
    (exchange, dedup_hash)). Resolves symbol → stock_id at insert time. Returns the
    number of NEW rows."""
    counts = counts if counts is not None else {}
    inserted = 0
    for row in rows:
        stock_id = resolve_stock_id(session, row.get("symbol"))
        if stock_id is None:
            counts["unmatched"] = counts.get("unmatched", 0) + 1
        stmt = (
            pg_insert(CorporateAnnouncement)
            .values(stock_id=stock_id, **row)
            .on_conflict_do_nothing(constraint="uq_corp_ann_exchange_hash")
            .returning(CorporateAnnouncement.id)
        )
        if session.execute(stmt).scalar() is not None:
            inserted += 1
    counts["added"] = counts.get("added", 0) + inserted
    return inserted


def _fetch_nse_announcements() -> list:
    return nse_session().get_json(NSE_ANNOUNCEMENTS, referer=NSE_ANN_REFERER)


def _fetch_bse_announcements(days: int = 3) -> dict:
    # BSE returns "No Record Found!" for an empty date window, so ask for the last few
    # days explicitly (YYYYMMDD). The feed pages; page 1 is the most recent.
    from datetime import date, timedelta
    today = date.today()
    return bse_session().get_json(
        BSE_ANNOUNCEMENTS,
        params={"pageno": "1", "strCat": "-1", "subcategory": "-1", "strScrip": "",
                "strSearch": "P", "strType": "C",
                "strPrevDate": (today - timedelta(days=days)).strftime("%Y%m%d"),
                "strToDate": today.strftime("%Y%m%d")},
        referer=f"{BSE_BASE}/",
        headers={"Origin": BSE_BASE},
    )


def fetch_announcements(exchanges=("NSE", "BSE"), verbose=True) -> dict:
    """Poll the configured exchange announcement feeds and store new rows. Daily cron."""
    with job_run("corporate_announcements", target=",".join(exchanges)) as (session, stats):
        counts = {"processed": 0, "added": 0, "unmatched": 0, "soft_fail": 0}
        if "NSE" in exchanges:
            try:
                rows = parse_nse_announcements(_fetch_nse_announcements())
                counts["processed"] += len(rows)
                upsert_announcements(session, rows, counts)
                session.commit()
                if verbose:
                    print(f"  NSE: {len(rows)} announcements parsed")
            except Exception as e:      # noqa: BLE001 — one feed down must not sink the other
                counts["soft_fail"] += 1
                if verbose:
                    print(f"  NSE announcements FAILED: {e}")
        if "BSE" in exchanges:
            try:
                rows = parse_bse_announcements(_fetch_bse_announcements())
                counts["processed"] += len(rows)
                upsert_announcements(session, rows, counts)
                session.commit()
                if verbose:
                    print(f"  BSE: {len(rows)} announcements parsed")
            except Exception as e:      # noqa: BLE001
                counts["soft_fail"] += 1
                if verbose:
                    print(f"  BSE announcements FAILED: {e}")
        stats.update(counts)
    return stats
