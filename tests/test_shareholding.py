"""Shareholding patterns: tolerant NSE parsing, quarterly dedup, bounded sweep."""

import json
from datetime import date
from pathlib import Path

from sqlalchemy import func, select

from db.models import JobRun, ShareholdingPattern
from engine.ingest import shareholding as shp
from factories import make_stock

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load(name):
    return json.loads((FIXTURES / name).read_text())


def test_parse_handles_drifted_keys_and_skips_periodless_rows():
    rows = shp.parse_nse_shareholding(_load("nse_shareholding.json"))
    assert len(rows) == 2                       # the empty-date record is dropped
    r0 = rows[0]
    assert r0["period_end"] == date(2025, 3, 31)
    assert (r0["promoter_pct"], r0["fii_pct"], r0["dii_pct"],
            r0["public_pct"], r0["pledged_pct"]) == (54.30, 12.10, 8.40, 25.20, 0.00)
    # second record uses the alternate key spellings, and omits pledged → NULL.
    r1 = rows[1]
    assert r1["period_end"] == date(2024, 12, 31)
    assert (r1["promoter_pct"], r1["fii_pct"], r1["dii_pct"]) == (55.0, 11.0, 9.0)
    assert r1["pledged_pct"] is None


def test_parse_skips_non_dict_and_periodless():
    assert shp.parse_nse_shareholding([123, {"promoter": "50"}]) == []


def test_upsert_dedup_on_stock_and_period(db_session):
    stock = make_stock(db_session, "ATHERENERG", nse_symbol="ATHERENERG")
    rows = shp.parse_nse_shareholding(_load("nse_shareholding.json"))
    assert shp.upsert_shareholding(db_session, stock.id, "ATHERENERG", rows) == 2
    db_session.commit()
    # same quarters again → no new rows.
    assert shp.upsert_shareholding(db_session, stock.id, "ATHERENERG", rows) == 0
    db_session.commit()
    assert db_session.scalar(select(func.count()).select_from(ShareholdingPattern)) == 2
    stored = db_session.scalar(
        select(ShareholdingPattern).where(ShareholdingPattern.period_end == date(2024, 12, 31))
    )
    assert stored.pledged_pct is None           # nullable persisted


def test_sweep_visits_only_active_universe(db_session, monkeypatch):
    make_stock(db_session, "ATHERENERG", nse_symbol="ATHERENERG")
    make_stock(db_session, "OLAELEC", nse_symbol="OLAELEC")
    make_stock(db_session, "PARKED", status="stale")     # excluded

    seen = []

    def fake(http, symbol):
        seen.append(symbol)
        return _load("nse_shareholding.json")

    monkeypatch.setattr(shp, "_fetch_shareholding", fake)
    stats = shp.fetch_shareholding(limit=50, verbose=False)

    assert set(seen) == {"ATHERENERG", "OLAELEC"}   # PARKED not swept
    assert stats["stocks"] == 2
    assert stats["added"] == 4                       # 2 quarters × 2 stocks
    job = db_session.scalar(
        select(JobRun).where(JobRun.job_type == "shareholding").order_by(JobRun.id.desc())
    )
    assert job.stats["added"] == 4


def test_sweep_counts_no_data_and_soft_fail(db_session, monkeypatch):
    make_stock(db_session, "EMPTYCO", nse_symbol="EMPTYCO")
    make_stock(db_session, "BROKENCO", nse_symbol="BROKENCO")

    def fake(http, symbol):
        if symbol == "BROKENCO":
            raise RuntimeError("feed error")
        return {"data": []}                          # empty → no_data

    monkeypatch.setattr(shp, "_fetch_shareholding", fake)
    stats = shp.fetch_shareholding(limit=50, verbose=False)
    assert stats["no_data"] == 1
    assert stats["soft_fail"] == 1
    assert stats["added"] == 0
