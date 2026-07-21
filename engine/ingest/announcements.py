"""
Corporate announcements — the NSE and BSE corporate-filings feeds, pulled directly.

NSE: https://www.nseindia.com/api/corporate-announcements?index=equities (JSON list,
needs the homepage cookie dance).
BSE: https://api.bseindia.com/BseIndiaAPI/api/XbrlAnnouncementCategory/w (JSON
{"Table": [...], "Table1": [{"ROWCNT": n}]}, needs a bseindia.com Referer/Origin).

The BSE feed is PAGED (~50 rows/page): fetching page 1 only truncated every busy day.
We loop pages until one comes back empty, so a day with hundreds of filings is fully
ingested; the shared `PoliteSession` paces the loop per-host. The predecessor endpoint
`AnnGetData/w` was retired by BSE (it now answers "No Record Found!" for every query);
`XbrlAnnouncementCategory/w` takes the same query params and returns the same row shape.

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
from engine.ingest.archive import archive_safe
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
BSE_ANNOUNCEMENTS = "https://api.bseindia.com/BseIndiaAPI/api/XbrlAnnouncementCategory/w"
BSE_ANN_REFERER = f"{BSE_BASE}/corporates/ann/"
BSE_ATTACH_BASE = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"

# Hard ceiling on the BSE page loop — a normal busy day is a handful of pages; this
# only guards against a feed that never returns an empty page.
BSE_MAX_PAGES = 50

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


def _bse_table(payload) -> list:
    """The row list out of one BSE page payload. An empty window answers with the
    bare string "No Record Found!" (or a Table-less dict), both of which yield []."""
    if isinstance(payload, dict):
        rows = payload.get("Table")
        return rows if isinstance(rows, list) else []
    return []


def _fetch_bse_page(http, pageno: int, prev_date: str, to_date: str) -> dict:
    # BSE returns "No Record Found!" for an empty date window, so ask for the window
    # explicitly (YYYYMMDD). strSearch=P selects by the date range; strType=C = equity.
    return http.get_json(
        BSE_ANNOUNCEMENTS,
        params={"pageno": str(pageno), "strCat": "-1", "subcategory": "-1",
                "strScrip": "", "strSearch": "P", "strType": "C",
                "strPrevDate": prev_date, "strToDate": to_date},
        referer=BSE_ANN_REFERER,
        headers={"Origin": BSE_BASE},
    )


def _fetch_bse_announcements(days: int = 3) -> dict:
    """Every BSE announcement in the last `days`, across ALL pages. The feed serves
    ~50 rows/page; we walk pages on one paced session until a page comes back empty,
    then return the accumulated rows as a single {"Table": [...]} payload so the
    parser and archive stay page-agnostic."""
    from datetime import date, timedelta
    today = date.today()
    prev_date = (today - timedelta(days=days)).strftime("%Y%m%d")
    to_date = today.strftime("%Y%m%d")
    http = bse_session()
    rows: list = []
    for pageno in range(1, BSE_MAX_PAGES + 1):
        page = _bse_table(_fetch_bse_page(http, pageno, prev_date, to_date))
        if not page:
            break
        rows.extend(page)
    return {"Table": rows}


def fetch_announcements(exchanges=("NSE", "BSE"), verbose=True) -> dict:
    """Poll the configured exchange announcement feeds and store new rows. Daily cron."""
    with job_run("corporate_announcements", target=",".join(exchanges)) as (session, stats):
        counts = {"processed": 0, "added": 0, "unmatched": 0, "soft_fail": 0,
                  "archive_failed": 0}
        if "NSE" in exchanges:
            try:
                raw = _fetch_nse_announcements()
                archive_safe("announcements", "NSE", raw, counts)
                rows = parse_nse_announcements(raw)
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
                raw = _fetch_bse_announcements()
                archive_safe("announcements", "BSE", raw, counts)
                rows = parse_bse_announcements(raw)
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
