"""
The corporate events calendar — NSE's forward-looking board-meeting / results
schedule, pulled directly, plus the event trigger that chases a universe stock's
results date the day it lands instead of waiting for the blind cron rotations.

Source: https://www.nseindia.com/api/event-calendar?index=equities — a JSON list of
{symbol, company, purpose, bm_desc, date} rows (needs the homepage cookie dance). The
feed re-serves the whole upcoming pipeline on every poll, so ingestion is idempotent:
each row dedups on (symbol, event_date, purpose_hash) and lands via ON CONFLICT DO
NOTHING. Symbols are resolved to our universe where they match; an unmatched row is kept
with stock_id NULL (a company we do not track still has a real meeting).

Two entry points, both cron jobs on the scheduler:
  - `fetch_calendar` (daily): refresh the calendar table.
  - `chase_events`   (every 2h during IST market+evening): for universe stocks whose
    results date is today/yesterday and not yet chased, fire a targeted refresh chain —
    feed-wide announcements for context, this stock's fresh filings, a snapshot+price
    refresh, and an analysis_queue row — then mark the event chased. No LLM call; the
    analyze job drains the queue when it next runs.

`_fetch_event_calendar` is the only network seam here; the chase reuses the existing
announcement/report/refresh fetchers (each its own seam), so tests drive the whole chain
without touching the network.
"""

import hashlib
import os
from datetime import datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from db.models import AnalysisQueue, CorporateEvent, Stock, utcnow
from engine.ingest.announcements import fetch_announcements
from engine.ingest.exchange_base import NSE_BASE, clean, nse_session, resolve_stock_id
from engine.ingest.reports import fetch_reports
from engine.ingest.yf_refresh import refresh_one
from engine.repo import job_run

NSE_EVENT_CALENDAR = f"{NSE_BASE}/api/event-calendar?index=equities"
NSE_EVENT_REFERER = f"{NSE_BASE}/companies-listing/corporate-filings-event-calendar"

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# Download budget for a single stock's fresh-filing chase — bounded so an unattended
# trigger can never fill the disk.
CHASE_REPORTS_BUDGET_MB = _env_int("CHASE_REPORTS_BUDGET_MB", 50)


def classify_event(purpose: str | None) -> str:
    """Map an event purpose to our event_type. A board meeting called to approve
    financial results is what the trigger acts on; everything else is 'other'."""
    c = (clean(purpose) or "").lower()
    return "results" if "result" in c else "other"


def _parse_event_date(v):
    """NSE serves the event date as '22-Jul-2026'. Returns a date, or None."""
    s = clean(v)
    if not s:
        return None
    for fmt in ("%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _purpose_hash(purpose: str | None) -> str:
    return hashlib.sha256((clean(purpose) or "").encode()).hexdigest()


def parse_event_calendar(payload) -> list[dict]:
    """NSE event-calendar JSON (a list of dicts) → normalized rows. A row with no
    symbol or no parseable date is dropped — an event we cannot key or schedule."""
    out = []
    for item in payload or []:
        if not isinstance(item, dict):
            continue
        symbol = clean(item.get("symbol"))
        event_date = _parse_event_date(item.get("date"))
        if symbol is None or event_date is None:
            continue
        purpose = clean(item.get("purpose"))
        out.append({
            "symbol": symbol,
            "event_type": classify_event(purpose),
            "event_date": event_date,
            "purpose_text": purpose,
            "purpose_hash": _purpose_hash(purpose),
            "source": "NSE",
            "raw": item,
        })
    return out


def upsert_events(session, rows: list[dict], counts: dict | None = None) -> int:
    """Insert normalized event rows, skipping any already seen (same
    (symbol, event_date, purpose_hash)). Resolves symbol → stock_id at insert time.
    Returns the number of NEW rows."""
    counts = counts if counts is not None else {}
    inserted = 0
    for row in rows:
        stock_id = resolve_stock_id(session, row.get("symbol"))
        if stock_id is None:
            counts["unmatched"] = counts.get("unmatched", 0) + 1
        stmt = (
            pg_insert(CorporateEvent)
            .values(stock_id=stock_id, **row)
            .on_conflict_do_nothing(constraint="uq_corp_event_symbol_date_purpose")
            .returning(CorporateEvent.id)
        )
        if session.execute(stmt).scalar() is not None:
            inserted += 1
    counts["added"] = counts.get("added", 0) + inserted
    return inserted


def _fetch_event_calendar() -> list:
    return nse_session().get_json(NSE_EVENT_CALENDAR, referer=NSE_EVENT_REFERER)


def fetch_calendar(verbose=True) -> dict:
    """Poll NSE's event-calendar feed and store new rows. Daily cron (append-only,
    deduped)."""
    with job_run("corporate_calendar", target="NSE") as (session, stats):
        counts = {"processed": 0, "added": 0, "unmatched": 0}
        rows = parse_event_calendar(_fetch_event_calendar())
        counts["processed"] = len(rows)
        upsert_events(session, rows, counts)
        session.commit()
        if verbose:
            print(f"  calendar: {counts['processed']} events parsed, "
                  f"{counts['added']} new ({counts['unmatched']} non-universe)")
        stats.update(counts)
    return stats


# ---------- event trigger ----------

def select_chase_events(session, today):
    """Universe results events whose date is today or yesterday and not yet chased —
    stock-resolved only (an unmatched calendar row has no snapshot to refresh)."""
    yesterday = today - timedelta(days=1)
    return session.scalars(
        select(CorporateEvent)
        .where(CorporateEvent.stock_id.isnot(None),
               CorporateEvent.event_type == "results",
               CorporateEvent.event_date.in_([yesterday, today]),
               CorporateEvent.chased_at.is_(None))
        .order_by(CorporateEvent.event_date, CorporateEvent.symbol)
    ).all()


def queue_analysis(session, stock_id: int, reason: str = "results") -> None:
    """Record that a stock's analysis should be refreshed. Just a queue row — no LLM
    call; the analyze job drains it, preferring queued stocks oldest-first."""
    session.add(AnalysisQueue(stock_id=stock_id, reason=reason, queued_at=utcnow()))


def chase_events(now=None, verbose=True) -> dict:
    """Fire the targeted refresh chain for universe stocks whose results date has just
    arrived. Feed-wide announcements once for context, then per stock: this stock's
    fresh filings, a snapshot+price refresh, an analysis_queue row, and the chased mark.
    A single soft failure in one step never sinks the others or the batch."""
    now = now or utcnow()
    today = now.date()
    since = datetime.combine(today - timedelta(days=1), time.min, tzinfo=timezone.utc)

    with job_run("event_chase", target="results/today+yesterday") as (session, stats):
        counts = {"events": 0, "stocks": 0, "announcements": 0, "reports": 0,
                  "refreshed": 0, "queued": 0, "failed": 0}
        events = select_chase_events(session, today)
        counts["events"] = len(events)
        if not events:
            stats.update(counts)
            return stats

        # (a) fresh announcements for context — feed-wide, once (not per symbol).
        try:
            fetch_announcements(verbose=False)
            counts["announcements"] = 1
        except Exception as e:      # noqa: BLE001 — context is best-effort
            counts["failed"] += 1
            if verbose:
                print(f"  chase announcements FAILED: {e}")

        by_stock: dict[int, list] = {}
        for ev in events:
            by_stock.setdefault(ev.stock_id, []).append(ev)
        counts["stocks"] = len(by_stock)

        for stock_id, evs in by_stock.items():
            stock = session.get(Stock, stock_id)
            if stock is None:
                continue
            symbol = stock.nse_symbol or stock.symbol

            # (b) this stock's fresh filings (feed-wide fetcher, symbol-filtered).
            try:
                fetch_reports(symbols=[symbol], since=since,
                              include_annual_reports=False,
                              budget_mb=CHASE_REPORTS_BUDGET_MB, verbose=False)
                counts["reports"] += 1
            except Exception as e:      # noqa: BLE001
                counts["failed"] += 1
                if verbose:
                    print(f"  chase reports {symbol} FAILED: {e}")

            # (c) refresh this stock's snapshot + prices.
            try:
                refresh_one(session, stock)
                session.commit()
                counts["refreshed"] += 1
            except Exception as e:      # noqa: BLE001
                session.rollback()
                counts["failed"] += 1
                if verbose:
                    print(f"  chase refresh {symbol} FAILED: {e}")

            # (d) mark analysis stale (queue only — no LLM) and (e) mark chased.
            queue_analysis(session, stock_id, "results")
            counts["queued"] += 1
            for ev in evs:
                ev.chased_at = now
            session.commit()

        if verbose:
            print(f"  chase: {counts['stocks']} stock(s), {counts['queued']} queued, "
                  f"{counts['refreshed']} refreshed, {counts['failed']} soft-fail")
        stats.update(counts)
    return stats
