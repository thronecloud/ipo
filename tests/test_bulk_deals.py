"""Bulk/block deals: NSE large-deal snapshot parsing, dedup, symbol resolution."""

import json
from datetime import date
from pathlib import Path

from sqlalchemy import func, select

from db.models import BulkDeal, JobRun
from engine.ingest import bulk_deals as bd
from factories import make_stock

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load(name):
    return json.loads((FIXTURES / name).read_text())


def test_parse_largedeals_normalizes_rows():
    rows = bd.parse_largedeals(_load("nse_largedeals.json"))
    # 3 valid bulk (all-null dropped) + 1 block
    assert len(rows) == 4
    bulk = [r for r in rows if r["source"] == "bulk"]
    block = [r for r in rows if r["source"] == "block"]
    assert len(bulk) == 3 and len(block) == 1
    first = bulk[0]
    assert first["symbol"] == "ATHERENERG"
    assert first["deal_date"] == date(2025, 5, 2)
    assert first["buy_sell"] == "BUY"
    assert first["quantity"] == 500000
    assert first["avg_price"] == 1234.56
    # "B" shorthand normalizes to BUY.
    assert next(r for r in bulk if r["symbol"] == "UNKNOWNSYM")["buy_sell"] == "BUY"


def test_buy_and_sell_of_same_size_are_distinct_rows(db_session):
    rows = bd.parse_largedeals(_load("nse_largedeals.json"))
    added = bd.upsert_bulk_deals(db_session, rows)
    db_session.commit()
    assert added == 4                        # side is part of the dedup key
    ather = db_session.scalars(
        select(BulkDeal).where(BulkDeal.symbol == "ATHERENERG", BulkDeal.source == "bulk")
    ).all()
    assert {r.buy_sell for r in ather} == {"BUY", "SELL"}


def test_upsert_is_idempotent(db_session):
    rows = bd.parse_largedeals(_load("nse_largedeals.json"))
    assert bd.upsert_bulk_deals(db_session, rows) == 4
    db_session.commit()
    assert bd.upsert_bulk_deals(db_session, rows) == 0
    db_session.commit()
    assert db_session.scalar(select(func.count()).select_from(BulkDeal)) == 4


def test_resolution_matched_and_unmatched(db_session):
    make_stock(db_session, "ATHERENERG", nse_symbol="ATHERENERG")
    counts = {}
    bd.upsert_bulk_deals(db_session, bd.parse_largedeals(_load("nse_largedeals.json")), counts)
    db_session.commit()
    matched = db_session.scalars(
        select(BulkDeal).where(BulkDeal.symbol == "ATHERENERG")
    ).all()
    assert matched and all(r.stock_id is not None for r in matched)
    unknown = db_session.scalar(select(BulkDeal).where(BulkDeal.symbol == "UNKNOWNSYM"))
    assert unknown.stock_id is None
    assert counts["unmatched"] == 1          # one unmatched symbol row


def test_fetch_bulk_deals_records_job_run(db_session, monkeypatch):
    monkeypatch.setattr(bd, "_fetch_largedeals", lambda: _load("nse_largedeals.json"))
    stats = bd.fetch_bulk_deals(verbose=False)
    assert stats["added"] == 4
    job = db_session.scalar(
        select(JobRun).where(JobRun.job_type == "bulk_deals").order_by(JobRun.id.desc())
    )
    assert job.stats["added"] == 4
