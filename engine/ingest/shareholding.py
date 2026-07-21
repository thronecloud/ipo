"""
Shareholding patterns — NSE's corporate shareholding feed, per active-universe stock.

Source: https://www.nseindia.com/api/corporate-share-holdings-pattern?index=equities&symbol=SYM
Quarterly data: one row per (stock, period_end), so a weekly sweep that re-sees an
already-stored quarter is a no-op (ON CONFLICT DO NOTHING). Percentages are parsed
tolerantly from the feed's category rows and left NULL when a category is absent; the
untouched source record is kept in `raw`. `_fetch_shareholding` is the network seam.
"""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from db.models import ShareholdingPattern, Stock
from engine.ingest.archive import archive_safe
from engine.ingest.exchange_base import NSE_BASE, clean, nse_session, to_float
from engine.repo import job_run

# PROVISIONAL: this path 404s from a datacenter IP as of 2026-07 (NSE appears to have
# moved shareholding behind the Akamai-guarded quote endpoint). Parser/storage/dedup
# below are source-shape-agnostic and fully tested; confirm/patch this URL on the box.
NSE_SHAREHOLDING = f"{NSE_BASE}/api/corporate-share-holdings-pattern"


def _shareholding_referer(symbol: str) -> str:
    return f"{NSE_BASE}/get-quotes/equity?symbol={symbol}"


# Candidate source keys per normalized field — the feed's naming has drifted across
# revisions, so we probe several rather than pin one.
_PERIOD_KEYS = ("date", "period", "periodEnd", "submissionDate", "asOnDate", "as_on")
_FIELD_KEYS = {
    "promoter_pct": ("promoter", "promoterPct", "promoterAndPromoterGroup",
                     "pr_and_prgrp", "promoters"),
    "fii_pct": ("fii", "fiiPct", "foreignInstitutions", "fpi", "foreignPortfolioInvestors"),
    "dii_pct": ("dii", "diiPct", "domesticInstitutions"),
    "public_pct": ("public", "publicPct", "publicShareholding", "publicAndOthers"),
    "pledged_pct": ("pledged", "pledgedPct", "promoterPledge", "encumbered",
                    "pledgedShares"),
}


def _pick(record: dict, keys) -> object:
    for k in keys:
        if k in record and record[k] not in (None, ""):
            return record[k]
    return None


def _parse_period(v):
    s = clean(v)
    if not s:
        return None
    s = s.replace("T", " ").split(".")[0].split(" ")[0]
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y", "%d %b %Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).date()
        except ValueError:
            continue
    return None


def _records(payload):
    """Extract the list of per-period filing records from an NSE shareholding payload."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "shareholdingPatterns", "records", "rows"):
            v = payload.get(key)
            if isinstance(v, list):
                return v
    return []


def parse_nse_shareholding(payload) -> list[dict]:
    """NSE shareholding JSON → normalized rows. A record with no parseable period is
    skipped (period_end is the natural key — a row without one has no identity)."""
    out = []
    for record in _records(payload):
        if not isinstance(record, dict):
            continue
        period_end = _parse_period(_pick(record, _PERIOD_KEYS))
        if period_end is None:
            continue
        row = {"period_end": period_end, "raw": record}
        for field, keys in _FIELD_KEYS.items():
            row[field] = to_float(_pick(record, keys))
        out.append(row)
    return out


def upsert_shareholding(session, stock_id: int, symbol: str | None,
                        rows: list[dict], counts: dict | None = None) -> int:
    """Insert normalized shareholding rows for a stock, skipping quarters already
    stored (same (stock_id, period_end)). Returns the number of NEW rows."""
    counts = counts if counts is not None else {}
    inserted = 0
    for row in rows:
        stmt = (
            pg_insert(ShareholdingPattern)
            .values(stock_id=stock_id, symbol=symbol, **row)
            .on_conflict_do_nothing(constraint="uq_shp_stock_period")
            .returning(ShareholdingPattern.id)
        )
        if session.execute(stmt).scalar() is not None:
            inserted += 1
    counts["added"] = counts.get("added", 0) + inserted
    return inserted


def _active_universe(session, limit: int):
    return session.scalars(
        select(Stock).where(Stock.status == "active").order_by(Stock.id).limit(limit)
    ).all()


def _fetch_shareholding(http, symbol: str):
    return http.get_json(NSE_SHAREHOLDING,
                         params={"index": "equities", "symbol": symbol},
                         referer=_shareholding_referer(symbol))


def fetch_shareholding(limit=100, verbose=True) -> dict:
    """Sweep the active universe (bounded), pulling each stock's shareholding pattern.
    Weekly cron. One stock's feed failing does not sink the sweep."""
    with job_run("shareholding", target=f"active/{limit}") as (session, stats):
        counts = {"stocks": 0, "added": 0, "no_data": 0, "soft_fail": 0,
                  "archive_failed": 0}
        http = nse_session()
        for stock in _active_universe(session, limit):
            symbol = stock.nse_symbol or stock.symbol
            counts["stocks"] += 1
            try:
                raw = _fetch_shareholding(http, symbol)
                archive_safe("shareholding", symbol, raw, counts)
                rows = parse_nse_shareholding(raw)
            except Exception as e:      # noqa: BLE001 — skip one, keep sweeping
                counts["soft_fail"] += 1
                if verbose:
                    print(f"  {symbol}: FAILED {e}")
                continue
            if not rows:
                counts["no_data"] += 1
                continue
            upsert_shareholding(session, stock.id, symbol, rows, counts)
            session.commit()
        stats.update(counts)
        if verbose:
            print(f"  shareholding: {counts['stocks']} stocks, {counts['added']} new rows")
    return stats
