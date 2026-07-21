"""
Autonomous report fetcher (item 9): pull the actual documents behind results and
annual-report announcements for our active universe, onto disk.

Sources of candidate documents:
  1. corporate_announcements rows (already resolved to a stock) whose category reads as
     a financial result or annual report and that carry an attachment URL,
  2. NSE's annual-reports endpoint per active stock.

Each document is written to data/reports/{SYMBOL}/{date}_{type}.{ext} and tracked in
corporate_filings. Idempotency + dedup: a (stock_id, source_url) already fetched is
never re-downloaded, and a document whose sha256 already exists for the stock (the same
PDF re-published under a new URL) is recognised and not stored twice. Two guards keep a
cron run from filling the disk:
  - a per-file size cap (SCRAPE_REPORT_MAX_MB, default 25) — a file over the cap is
    recorded as skipped_cap and never retried,
  - a per-run download budget (SCRAPE_REPORTS_BUDGET_MB, default 200) — once spent,
    remaining candidates are left for the next run (no row written, so they retry).

Runs weekly for the full universe and daily against just-announced results (`since`).
`_download_file` and `_fetch_annual_reports` are the only network seams.
"""

import hashlib
import os
import re
from datetime import datetime, timezone

from sqlalchemy import exists, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from db.models import CorporateAnnouncement, CorporateFiling, Stock
from engine.ingest.announcements import ANNUAL_REPORT_KEYWORDS, RESULTS_KEYWORDS
from engine.ingest.exchange_base import NSE_BASE, clean, nse_session
from engine.repo import job_run

REPORTS_DIR = "data/reports"
NSE_ANNUAL_REPORTS = f"{NSE_BASE}/api/annual-reports"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


MAX_FILE_MB = _env_int("SCRAPE_REPORT_MAX_MB", 25)
BUDGET_MB = _env_int("SCRAPE_REPORTS_BUDGET_MB", 200)
_MB = 1024 * 1024


def classify(category: str | None) -> str | None:
    """Map an announcement category to a downloadable filing type, or None if it is
    neither a result nor an annual report."""
    c = (clean(category) or "").lower()
    if not c:
        return None
    if any(k in c for k in ANNUAL_REPORT_KEYWORDS):
        return "annual_report"
    if any(k in c for k in RESULTS_KEYWORDS):
        return "results"
    return None


def _ext(url: str) -> str:
    m = re.search(r"\.([A-Za-z0-9]{1,5})(?:\?|$)", url or "")
    return m.group(1).lower() if m else "pdf"


def _safe(part: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", (part or "x"))


def _dest_path(cand: dict) -> str:
    symbol = _safe(cand.get("symbol") or "UNKNOWN")
    period = _safe((cand.get("period") or "unknown")[:32])
    ftype = _safe(cand.get("filing_type") or "doc")
    return os.path.join(REPORTS_DIR, symbol, f"{period}_{ftype}.{_ext(cand['source_url'])}")


def _candidates_from_announcements(session, since, stock_ids) -> list[dict]:
    q = (
        select(CorporateAnnouncement)
        .where(CorporateAnnouncement.stock_id.in_(stock_ids),
               CorporateAnnouncement.attachment_url.isnot(None))
        .order_by(CorporateAnnouncement.announced_at.desc().nullslast())
    )
    if since is not None:
        q = q.where(CorporateAnnouncement.announced_at >= since)
    out = []
    for a in session.scalars(q).all():
        ftype = classify(a.category)
        if ftype is None:
            continue
        period = a.announced_at.date().isoformat() if a.announced_at else "unknown"
        out.append({"stock_id": a.stock_id, "symbol": a.symbol,
                    "filing_type": ftype, "period": period,
                    "source_url": a.attachment_url})
    return out


def parse_annual_reports(payload, stock_id: int, symbol: str | None) -> list[dict]:
    data = payload.get("data") if isinstance(payload, dict) else payload
    out = []
    for item in data or []:
        if not isinstance(item, dict):
            continue
        url = clean(item.get("fileName") or item.get("file") or item.get("companyName"))
        if not url or not url.lower().startswith("http"):
            continue
        from_yr, to_yr = clean(item.get("fromYr")), clean(item.get("toYr"))
        period = f"{from_yr or '?'}-{to_yr or '?'}"
        out.append({"stock_id": stock_id, "symbol": symbol,
                    "filing_type": "annual_report", "period": period,
                    "source_url": url})
    return out


def _already_fetched(session, stock_id: int, source_url: str) -> bool:
    return bool(session.scalar(
        select(exists().where(CorporateFiling.stock_id == stock_id,
                              CorporateFiling.source_url == source_url))
    ))


def _sha_exists(session, stock_id: int, sha256: str) -> bool:
    return bool(session.scalar(
        select(exists().where(CorporateFiling.stock_id == stock_id,
                              CorporateFiling.sha256 == sha256))
    ))


def _record(session, cand: dict, *, status: str, local_path=None,
            sha256=None, size=None):
    stmt = (
        pg_insert(CorporateFiling)
        .values(stock_id=cand["stock_id"], symbol=cand.get("symbol"),
                filing_type=cand.get("filing_type"), period=cand.get("period"),
                source_url=cand["source_url"], local_path=local_path,
                sha256=sha256, size_bytes=size, status=status,
                fetched_at=datetime.now(timezone.utc))
        .on_conflict_do_nothing(constraint="uq_filing_stock_url")
    )
    session.execute(stmt)


def _download_file(http, url: str, dest: str, max_bytes: int):
    """Download a document, enforcing the per-file cap. Returns (sha256, size) on
    success, or None if the file exceeds `max_bytes` (nothing written)."""
    resp = http.get(url)
    resp.raise_for_status()
    content = resp.content
    if len(content) > max_bytes:
        return None
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as f:
        f.write(content)
    return hashlib.sha256(content).hexdigest(), len(content)


def _fetch_annual_reports(http, symbol: str):
    return http.get_json(NSE_ANNUAL_REPORTS,
                         params={"index": "equities", "symbol": symbol},
                         referer=f"{NSE_BASE}/get-quotes/equity?symbol={symbol}")


def fetch_reports(limit=200, budget_mb=None, since=None, include_annual_reports=True,
                  verbose=True) -> dict:
    """Download report documents for the active universe. `since` restricts the
    announcement candidates to those announced at/after it (the fresh-results trigger);
    None sweeps everything. `limit` bounds how many active stocks are considered."""
    budget_bytes = (BUDGET_MB if budget_mb is None else budget_mb) * _MB
    max_file_bytes = MAX_FILE_MB * _MB
    with job_run("corporate_filings", target=f"active/{limit}") as (session, stats):
        counts = {"candidates": 0, "downloaded": 0, "duplicate": 0, "skipped_existing": 0,
                  "skipped_cap": 0, "skipped_budget": 0, "failed": 0, "bytes": 0}
        stocks = session.scalars(
            select(Stock).where(Stock.status == "active").order_by(Stock.id).limit(limit)
        ).all()
        stock_ids = [s.id for s in stocks]
        if not stock_ids:
            stats.update(counts)
            return stats

        candidates = _candidates_from_announcements(session, since, stock_ids)

        http = nse_session()
        if include_annual_reports:
            for stock in stocks:
                symbol = stock.nse_symbol or stock.symbol
                try:
                    payload = _fetch_annual_reports(http, symbol)
                    candidates += parse_annual_reports(payload, stock.id, symbol)
                except Exception as e:      # noqa: BLE001 — one stock's feed, skip it
                    if verbose:
                        print(f"  annual-reports {symbol}: FAILED {e}")

        counts["candidates"] = len(candidates)
        spent = 0
        for cand in candidates:
            if _already_fetched(session, cand["stock_id"], cand["source_url"]):
                counts["skipped_existing"] += 1
                continue
            if spent >= budget_bytes:
                counts["skipped_budget"] += 1   # no row — retried next run
                continue
            dest = _dest_path(cand)
            try:
                result = _download_file(http, cand["source_url"], dest, max_file_bytes)
            except Exception as e:      # noqa: BLE001 — no row, retried next run
                counts["failed"] += 1
                if verbose:
                    print(f"  download FAILED {cand['source_url']}: {e}")
                continue
            if result is None:
                _record(session, cand, status="skipped_cap")   # permanent — too big
                counts["skipped_cap"] += 1
                session.commit()
                continue
            sha256, size = result
            spent += size
            if _sha_exists(session, cand["stock_id"], sha256):
                _remove(dest)
                _record(session, cand, status="duplicate", sha256=sha256, size=size)
                counts["duplicate"] += 1
                session.commit()
                continue
            _record(session, cand, status="downloaded", local_path=dest,
                    sha256=sha256, size=size)
            counts["downloaded"] += 1
            counts["bytes"] += size
            session.commit()
        stats.update(counts)
        if verbose:
            print(f"  reports: {counts['downloaded']} downloaded, "
                  f"{counts['duplicate']} dup, {counts['skipped_budget']} over budget "
                  f"({counts['bytes'] // _MB} MB spent)")
    return stats


def _remove(path: str):
    try:
        os.remove(path)
    except OSError:
        pass
