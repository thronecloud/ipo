"""
Freshness SLOs — the per-dataset staleness contracts.

Covers:
  1. trading-day lag arithmetic (pure), including the weekend case — a Friday bar
     read on Saturday is zero trading days stale, not one.
  2. per-dataset compliance + worst-offender selection over seeded fresh/stale
     populations, in a single grouped scan (never priced counts as the worst offender).
  3. the API contract: GET /api/admin/scheduler carries the `slos` block.
"""

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from api.main import app
from db.models import (
    CorporateAnnouncement,
    DailyPrice,
    IndexPrice,
    JobRun,
    ShareholdingPattern,
    StockSnapshot,
)
from engine.quality.slo import (
    SLO_SPECS,
    _trading_calendar,
    _trading_lag,
    compute_slos,
)
from factories import make_stock

UTC = timezone.utc

# A fixed Friday reference so trading-day math is deterministic.
#   2026-07-13 Mon … 2026-07-17 Fri, 18 Sat, 19 Sun, 20 Mon.
NOW = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
MON, TUE, WED, THU, FRI = (date(2026, 7, d) for d in (13, 14, 15, 16, 17))


def _report(slos: list[dict], dataset: str) -> dict:
    return next(s for s in slos if s["dataset"] == dataset)


def _add_bars(session, stock, dates):
    for d in dates:
        session.add(DailyPrice(stock_id=stock.id, date=d, close=100.0))
    session.commit()


# ---------- trading-day arithmetic (pure) ----------

def test_trading_calendar_skips_weekends_but_synthesizes_outage_weekdays():
    known = [MON, TUE, WED, THU, FRI]
    # Read on the Saturday: the week's weekend adds nothing.
    cal = _trading_calendar(known, date(2026, 7, 18))
    assert date(2026, 7, 18) not in cal and date(2026, 7, 19) not in cal
    # Read on the following Monday: the Monday is synthesized even with no bar,
    # so an outage past Friday is visible rather than masked.
    cal2 = _trading_calendar(known, date(2026, 7, 20))
    assert date(2026, 7, 20) in cal2


def test_trading_lag_weekend_case():
    known = [MON, TUE, WED, THU, FRI]
    cal_sat = _trading_calendar(known, date(2026, 7, 18))
    # Friday bar read on Saturday → 0 trading days stale (not the 1 calendar day).
    assert _trading_lag(FRI, cal_sat, date(2026, 7, 18)) == 0
    # Wednesday bar read on Friday → 2 trading days.
    assert _trading_lag(WED, cal_sat, FRI) == 2
    # Friday bar read the next Monday → 1 trading day (Monday is a trading day).
    cal_mon = _trading_calendar(known, date(2026, 7, 20))
    assert _trading_lag(FRI, cal_mon, date(2026, 7, 20)) == 1


# ---------- price bars: fresh vs stale population ----------

def test_price_bars_compliance_and_offenders(db_session):
    fresh = make_stock(db_session, "FRESH")
    stale = make_stock(db_session, "STALE")
    never = make_stock(db_session, "NEVER")                       # active, no bars
    parked = make_stock(db_session, "PARKED", status="unfetchable")

    _add_bars(db_session, fresh, [WED, THU, FRI])                 # newest = Fri (now) → lag 0
    _add_bars(db_session, stale, [MON, TUE])                      # newest = Tue → lag 3
    _add_bars(db_session, parked, [MON])                          # excluded (not active)

    rep = _report(compute_slos(db_session, NOW), "price_bars")

    assert rep["population"] == 3                                 # FRESH, STALE, NEVER only
    assert rep["compliant"] == 1                                  # only FRESH
    assert rep["compliance_pct"] == pytest.approx(33.3, abs=0.1)
    assert rep["breached"] is True
    assert rep["missing"] == 1                                    # NEVER
    assert rep["worst_lag"] == 3                                  # STALE
    # Never-fetched sorts to the very top, then descending lag.
    labels = [o["label"] for o in rep["offenders"]]
    assert labels[0] == "NEVER"
    never_o = next(o for o in rep["offenders"] if o["label"] == "NEVER")
    assert never_o["missing"] is True and never_o["lag"] is None
    assert {"STALE", "NEVER"} <= set(labels)
    assert "FRESH" not in labels                                  # compliant, not an offender


def test_price_bars_all_fresh_is_green(db_session):
    a = make_stock(db_session, "AAA")
    b = make_stock(db_session, "BBB")
    _add_bars(db_session, a, [THU, FRI])
    _add_bars(db_session, b, [WED, THU, FRI])
    rep = _report(compute_slos(db_session, NOW), "price_bars")
    assert rep["compliance_pct"] == 100.0
    assert rep["breached"] is False
    assert rep["offenders"] == []


# ---------- snapshots: calendar-day age ----------

def test_snapshots_calendar_age(db_session):
    fresh = make_stock(db_session, "SNAPOK")
    stale = make_stock(db_session, "SNAPOLD")
    make_stock(db_session, "SNAPNONE")                           # active, no snapshot
    db_session.add(StockSnapshot(stock_id=fresh.id, source="yfinance",
                                 captured_at=NOW - timedelta(days=1), content_hash="a"))
    db_session.add(StockSnapshot(stock_id=stale.id, source="yfinance",
                                 captured_at=NOW - timedelta(days=10), content_hash="b"))
    # A screener snapshot must not count toward the yfinance SLO.
    db_session.add(StockSnapshot(stock_id=stale.id, source="screener",
                                 captured_at=NOW, content_hash="c"))
    db_session.commit()

    rep = _report(compute_slos(db_session, NOW), "snapshots")
    assert rep["population"] == 3
    assert rep["compliant"] == 1                                 # only SNAPOK (1d ≤ 7d)
    assert rep["missing"] == 1                                   # SNAPNONE
    offenders = {o["label"] for o in rep["offenders"]}
    assert {"SNAPOLD", "SNAPNONE"} == offenders


# ---------- shareholding: population = active WITH a pattern ----------

def test_shareholding_excludes_stocks_without_a_pattern(db_session):
    has = make_stock(db_session, "SHOK")
    old = make_stock(db_session, "SHOLD")
    make_stock(db_session, "SHNONE")                            # no pattern → out of scope
    db_session.add(ShareholdingPattern(stock_id=has.id,
                                       period_end=NOW.date() - timedelta(days=100)))
    db_session.add(ShareholdingPattern(stock_id=old.id,
                                       period_end=NOW.date() - timedelta(days=200)))
    db_session.commit()

    rep = _report(compute_slos(db_session, NOW), "shareholding")
    assert rep["population"] == 2                                # SHNONE excluded
    assert rep["compliant"] == 1                                # 100d ≤ 136d; 200d is not
    assert [o["label"] for o in rep["offenders"]] == ["SHOLD"]


# ---------- index prices: trading-day lag ----------

def test_index_prices_trading_lag(db_session):
    for d in (WED, THU, FRI):
        db_session.add(IndexPrice(symbol="^CNXSC", date=d, close=1.0))
    for d in (MON, TUE):
        db_session.add(IndexPrice(symbol="^NSEI", date=d, close=1.0))
    db_session.commit()
    rep = _report(compute_slos(db_session, NOW), "index_prices")
    assert rep["population"] == 2
    assert rep["compliant"] == 1                                # ^CNXSC fresh, ^NSEI 3d stale
    assert [o["label"] for o in rep["offenders"]] == ["^NSEI"]


# ---------- announcements + dq_audit: singleton recency ----------

def test_announcements_recency(db_session):
    db_session.add(CorporateAnnouncement(exchange="NSE", dedup_hash="x",
                                         announced_at=NOW - timedelta(hours=5)))
    db_session.commit()
    rep = _report(compute_slos(db_session, NOW), "announcements")
    assert rep["population"] == 1 and rep["compliant"] == 1
    assert rep["breached"] is False


def test_announcements_stale_is_breached(db_session):
    db_session.add(CorporateAnnouncement(exchange="NSE", dedup_hash="x",
                                         announced_at=NOW - timedelta(days=3)))
    db_session.commit()
    rep = _report(compute_slos(db_session, NOW), "announcements")
    assert rep["compliant"] == 0 and rep["breached"] is True


def test_dq_audit_recency(db_session):
    db_session.add(JobRun(job_type="dq_audit", status="success",
                          started_at=NOW - timedelta(days=1),
                          finished_at=NOW - timedelta(days=1)))
    db_session.commit()
    rep = _report(compute_slos(db_session, NOW), "dq_audit")
    assert rep["compliant"] == 1 and rep["breached"] is False


def test_dq_audit_missing_run_is_breached(db_session):
    rep = _report(compute_slos(db_session, NOW), "dq_audit")
    assert rep["missing"] == 1 and rep["breached"] is True


# ---------- empty population is n/a, not a breach ----------

def test_empty_population_is_not_a_breach(db_session):
    rep = _report(compute_slos(db_session, NOW), "price_bars")
    assert rep["population"] == 0
    assert rep["compliance_pct"] is None
    assert rep["breached"] is False


# ---------- API contract ----------

@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def test_scheduler_endpoint_carries_slos_block(client, db_session):
    a = make_stock(db_session, "APIOK")
    _add_bars(db_session, a, [WED, THU, FRI])
    r = client.get("/api/admin/scheduler")
    assert r.status_code == 200
    body = r.json()
    assert "slos" in body
    datasets = {s["dataset"] for s in body["slos"]}
    assert datasets == {spec.dataset for spec in SLO_SPECS}
    for s in body["slos"]:
        assert s["description"] and s["target"] and s["unit"]
        assert set(s) >= {
            "dataset", "description", "target", "unit", "objective_pct",
            "population", "compliant", "compliance_pct", "worst_lag",
            "missing", "breached", "offenders",
        }
