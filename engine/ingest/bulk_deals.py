"""
Bulk & block deals — NSE's after-close large-deal snapshot, pulled directly.

Source: https://www.nseindia.com/api/snapshot-capital-market-largedeal — one JSON with
BULK_DEALS_DATA and BLOCK_DEALS_DATA for the latest session. It re-serves the same
session's deals on every poll, so each row carries a content dedup hash
(date+symbol+client+side+quantity+source) and lands via ON CONFLICT DO NOTHING. Daily
cron after market close. The `_fetch_*` seam is the only network touch.
"""

import hashlib
from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert

from db.models import BulkDeal
from engine.ingest.archive import archive_safe
from engine.ingest.exchange_base import (
    NSE_BASE,
    clean,
    nse_session,
    resolve_stock_id,
    to_float,
    to_int,
)
from engine.repo import job_run

NSE_LARGEDEALS = f"{NSE_BASE}/api/snapshot-capital-market-largedeal"
NSE_LARGEDEALS_REFERER = f"{NSE_BASE}/market-data/large-deals"


def _parse_deal_date(v):
    s = clean(v)
    if not s:
        return None
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).date()
        except ValueError:
            continue
    return None


def _normalize_side(v):
    s = (clean(v) or "").upper()
    if s.startswith("B"):
        return "BUY"
    if s.startswith("S"):
        return "SELL"
    return clean(v)


def _dedup_hash(source, deal_date, symbol, client, side, qty) -> str:
    basis = f"{source}|{deal_date}|{symbol}|{client}|{side}|{qty}"
    return hashlib.sha256(basis.encode()).hexdigest()


def _parse_rows(items, source: str) -> list[dict]:
    out = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        symbol = clean(item.get("symbol") or item.get("BD_SYMBOL"))
        client = clean(item.get("name") or item.get("BD_CLIENT_NAME"))
        if symbol is None and client is None:
            continue
        deal_date = _parse_deal_date(item.get("date") or item.get("BD_DT_DATE"))
        side = _normalize_side(item.get("buySell") or item.get("BD_BUY_SELL"))
        qty = to_int(item.get("qty") or item.get("BD_QTY_TRD"))
        avg_price = to_float(item.get("watp") or item.get("BD_TP_WATP"))
        out.append({
            "exchange": "NSE",
            "source": source,
            "symbol": symbol,
            "deal_date": deal_date,
            "client_name": client,
            "buy_sell": side,
            "quantity": qty,
            "avg_price": avg_price,
            "dedup_hash": _dedup_hash(source, deal_date, symbol, client, side, qty),
            "raw": item,
        })
    return out


def parse_largedeals(payload) -> list[dict]:
    """NSE large-deal snapshot JSON → normalized bulk+block deal rows."""
    if not isinstance(payload, dict):
        return []
    rows = _parse_rows(payload.get("BULK_DEALS_DATA"), "bulk")
    rows += _parse_rows(payload.get("BLOCK_DEALS_DATA"), "block")
    return rows


def upsert_bulk_deals(session, rows: list[dict], counts: dict | None = None) -> int:
    """Insert normalized deal rows, skipping any already stored (same dedup_hash).
    Resolves symbol → stock_id. Returns the number of NEW rows."""
    counts = counts if counts is not None else {}
    inserted = 0
    for row in rows:
        stock_id = resolve_stock_id(session, row.get("symbol"))
        if stock_id is None:
            counts["unmatched"] = counts.get("unmatched", 0) + 1
        stmt = (
            pg_insert(BulkDeal)
            .values(stock_id=stock_id, **row)
            .on_conflict_do_nothing(constraint="uq_bulk_deal_hash")
            .returning(BulkDeal.id)
        )
        if session.execute(stmt).scalar() is not None:
            inserted += 1
    counts["added"] = counts.get("added", 0) + inserted
    return inserted


def _fetch_largedeals() -> dict:
    return nse_session().get_json(NSE_LARGEDEALS, referer=NSE_LARGEDEALS_REFERER)


def fetch_bulk_deals(verbose=True) -> dict:
    """Poll NSE's large-deal snapshot and store new bulk/block deals. Daily cron."""
    with job_run("bulk_deals", target="NSE") as (session, stats):
        counts = {"processed": 0, "added": 0, "unmatched": 0, "archive_failed": 0}
        raw = _fetch_largedeals()
        archive_safe("bulk_deals", "NSE", raw, counts)
        rows = parse_largedeals(raw)
        counts["processed"] = len(rows)
        upsert_bulk_deals(session, rows, counts)
        session.commit()
        if verbose:
            print(f"  NSE large deals: {len(rows)} parsed, {counts['added']} new")
        stats.update(counts)
    return stats
