"""Cross-source reconciliation: the comparator, its tolerances, discrepancy dedup /
resolution, the rolling trust math, the wholesale-failure alert, and the admin contract.

The engine now ingests the same fact from more than one source; these tests pin that the
comparator agrees when they agree, flags when they diverge, skips a one-sided or stale
fact, and never double-records the same disagreement.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from api.main import app
from db.models import (
    DailyPrice,
    JobRun,
    ShareholdingPattern,
    SourceDiscrepancy,
    SourceTrust,
)
from engine.quality import reconcile as R
from engine.repo import add_snapshot, extract_columns
from factories import make_stock

UTC = timezone.utc


def _now():
    return datetime.now(UTC)


def _days(n):
    return _now() + timedelta(days=n)


# ---------- seeding helpers ----------

def _yf(session, stock, *, captured_at, info=None, last_date=None, last_close=None):
    info = info or {"regularMarketPrice": 100.0}
    hist = {}
    if last_date is not None:
        hist = {"last_date": last_date, "last_close": last_close}
    payload = {"info": info, "history_summary": hist}
    snap, _ = add_snapshot(session, stock, payload, extract_columns(info),
                           source="yfinance", data_quality="full",
                           captured_at=captured_at)
    session.commit()
    return snap


def _screener(session, stock, *, captured_at, promoters):
    """promoters: {'Sep 2025': 72.5, ...}"""
    payload = {"screener": {"shareholding": {"Promoters": promoters}}}
    snap, _ = add_snapshot(session, stock, payload, {},
                           source="screener", data_quality="full",
                           captured_at=captured_at)
    session.commit()
    return snap


def _bar(session, stock, d, close):
    session.add(DailyPrice(stock_id=stock.id, date=d, close=close))
    session.commit()


# ============================================================
# pure comparison core — agree / diverge / edge / missing side
# ============================================================

def test_compare_relative_agrees_within_tolerance():
    # symmetric relative diff divides by the larger value: 0.4/100.4 → 0.398%.
    c = R.compare_relative("price", "k", "bhavcopy", 100.0, "yfinance", 100.4, 0.5)
    assert c is not None and c.agree is True
    assert c.divergence == pytest.approx(100 * 0.4 / 100.4, abs=1e-9)


def test_compare_relative_diverges_beyond_tolerance():
    c = R.compare_relative("price", "k", "bhavcopy", 100.0, "yfinance", 101.0, 0.5)
    assert c is not None and c.agree is False
    assert c.divergence == pytest.approx(100 * 1.0 / 101.0, abs=1e-9)


def test_compare_relative_edge_is_inclusive():
    # 99.5 vs 100.0 is exactly 0.5% (0.5/100) → on the tolerance, agrees (<=).
    on = R.compare_relative("price", "k", "a", 99.5, "b", 100.0, 0.5)
    assert on.agree is True and on.divergence == pytest.approx(0.5, abs=1e-9)
    # 99.4 vs 100.0 is 0.6% → just past, diverges.
    past = R.compare_relative("price", "k", "a", 99.4, "b", 100.0, 0.5)
    assert past.agree is False


def test_compare_missing_side_is_not_comparable():
    assert R.compare_relative("price", "k", "a", None, "b", 100.0, 0.5) is None
    assert R.compare_relative("price", "k", "a", 100.0, "b", None, 0.5) is None
    assert R.compare_absolute("promoter_pct", "k", "a", None, "b", 50.0, 1.0) is None


def test_compare_absolute_points_agree_diverge_edge():
    assert R.compare_absolute("promoter_pct", "k", "nse", 72.5, "screener", 73.4, 1.0).agree
    assert not R.compare_absolute("promoter_pct", "k", "nse", 72.0, "screener", 74.0, 1.0).agree
    edge = R.compare_absolute("promoter_pct", "k", "nse", 72.0, "screener", 73.0, 1.0)
    assert edge.agree and edge.divergence == pytest.approx(1.0, abs=1e-9)


# ============================================================
# price comparator (bhavcopy bar vs yfinance snapshot close)
# ============================================================

def test_price_check_agrees_within_half_percent(db_session):
    stock = make_stock(db_session, "PXAGREE")
    d = _days(-1).date()
    _yf(db_session, stock, captured_at=_days(-1), last_date=d.isoformat(), last_close=200.0)
    _bar(db_session, stock, d, 200.5)   # 0.25% apart
    kind, comp = R.price_check(db_session, stock.id, _now())
    assert kind == "compared" and comp.agree is True
    assert comp.fact == "price" and comp.source_a == "bhavcopy" and comp.source_b == "yfinance"


def test_price_check_flags_divergence_beyond_half_percent(db_session):
    stock = make_stock(db_session, "PXDIV")
    d = _days(-1).date()
    _yf(db_session, stock, captured_at=_days(-1), last_date=d.isoformat(), last_close=200.0)
    _bar(db_session, stock, d, 210.0)   # 5% apart
    kind, comp = R.price_check(db_session, stock.id, _now())
    assert kind == "compared" and comp.agree is False
    assert comp.divergence == pytest.approx(100 * 10 / 210, abs=1e-6)


def test_price_check_missing_when_no_bhavcopy_bar(db_session):
    stock = make_stock(db_session, "PXNOBAR")
    d = _days(-1).date()
    _yf(db_session, stock, captured_at=_days(-1), last_date=d.isoformat(), last_close=200.0)
    # no daily bar seeded for that date
    kind, comp = R.price_check(db_session, stock.id, _now())
    assert kind == "missing" and comp is None


def test_price_check_stale_when_snapshot_old(db_session):
    stock = make_stock(db_session, "PXSTALE")
    d = _days(-10).date()
    _yf(db_session, stock, captured_at=_days(-10), last_date=d.isoformat(), last_close=200.0)
    _bar(db_session, stock, d, 200.1)
    kind, comp = R.price_check(db_session, stock.id, _now())
    assert kind == "stale" and comp is None


# ============================================================
# market-cap comparator (computed close×shares vs published cap)
# ============================================================

def test_market_cap_agree_and_diverge(db_session):
    agree = make_stock(db_session, "MCAGREE")
    _yf(db_session, agree, captured_at=_days(-1),
        info={"regularMarketPrice": 100.0, "sharesOutstanding": 500_000_000,
              "marketCap": 50_000_000_000})   # computed 50e9 == published
    kind, comp = R.market_cap_check(db_session, agree.id, _now())
    assert kind == "compared" and comp.agree is True
    assert comp.source_a == "computed" and comp.source_b == "yfinance"

    div = make_stock(db_session, "MCDIV")
    _yf(db_session, div, captured_at=_days(-1),
        info={"regularMarketPrice": 120.0, "sharesOutstanding": 500_000_000,
              "marketCap": 50_000_000_000})   # computed 60e9 vs 50e9 → ~16.7%
    kind, comp = R.market_cap_check(db_session, div.id, _now())
    assert kind == "compared" and comp.agree is False


def test_market_cap_missing_without_shares(db_session):
    stock = make_stock(db_session, "MCNOSH")
    _yf(db_session, stock, captured_at=_days(-1),
        info={"regularMarketPrice": 100.0, "marketCap": 50_000_000_000})
    kind, comp = R.market_cap_check(db_session, stock.id, _now())
    assert kind == "missing" and comp is None


# ============================================================
# promoter comparator (NSE pattern vs screener scrape, same quarter)
# ============================================================

def _seed_promoter(session, stock, *, nse_pct, screener_pct,
                   period_end, screener_period, nse_fetched, scr_captured):
    session.add(ShareholdingPattern(
        stock_id=stock.id, symbol=stock.symbol, period_end=period_end,
        promoter_pct=nse_pct, fetched_at=nse_fetched,
    ))
    session.commit()
    _screener(session, stock, captured_at=scr_captured,
              promoters={screener_period: screener_pct})


def test_promoter_agrees_on_common_quarter(db_session):
    stock = make_stock(db_session, "SHAGREE")
    _seed_promoter(db_session, stock, nse_pct=72.50, screener_pct=72.96,
                   period_end=datetime(2025, 9, 30).date(), screener_period="Sep 2025",
                   nse_fetched=_days(-1), scr_captured=_days(-1))
    kind, comp = R.promoter_check(db_session, stock.id, _now())
    assert kind == "compared" and comp.agree is True
    assert comp.fact_key == "2025Q3" and comp.source_a == "nse" and comp.source_b == "screener"


def test_promoter_flags_point_divergence(db_session):
    stock = make_stock(db_session, "SHDIV")
    _seed_promoter(db_session, stock, nse_pct=60.0, screener_pct=72.96,
                   period_end=datetime(2025, 9, 30).date(), screener_period="Sep 2025",
                   nse_fetched=_days(-1), scr_captured=_days(-1))
    kind, comp = R.promoter_check(db_session, stock.id, _now())
    assert kind == "compared" and comp.agree is False
    assert comp.divergence == pytest.approx(12.96, abs=1e-6)


def test_promoter_missing_without_common_quarter(db_session):
    stock = make_stock(db_session, "SHNOCOMMON")
    # NSE holds Sep 2025, screener holds Mar 2025 — no overlap.
    _seed_promoter(db_session, stock, nse_pct=72.0, screener_pct=72.0,
                   period_end=datetime(2025, 9, 30).date(), screener_period="Mar 2025",
                   nse_fetched=_days(-1), scr_captured=_days(-1))
    kind, comp = R.promoter_check(db_session, stock.id, _now())
    assert kind == "missing" and comp is None


def test_promoter_stale_when_screener_old(db_session):
    stock = make_stock(db_session, "SHSTALE")
    _seed_promoter(db_session, stock, nse_pct=72.5, screener_pct=90.0,
                   period_end=datetime(2025, 9, 30).date(), screener_period="Sep 2025",
                   nse_fetched=_days(-1), scr_captured=_days(-30))
    kind, comp = R.promoter_check(db_session, stock.id, _now())
    assert kind == "stale" and comp is None


# ============================================================
# discrepancy persistence — dedup, resolve, reopen
# ============================================================

def _price_comp(diverge=True):
    b = 210.0 if diverge else 200.5
    return R.compare_relative("price", "2025-09-30", "bhavcopy", b, "yfinance", 200.0, 0.5)


def test_same_discrepancy_recorded_twice_is_one_row(db_session):
    stock = make_stock(db_session, "DEDUP")
    now = _now()
    assert R.record_discrepancy(db_session, stock.id, _price_comp(), now) is True
    db_session.commit()
    # second detection of the SAME (stock, fact, key) is not new, and adds no row.
    assert R.record_discrepancy(db_session, stock.id, _price_comp(), now) is False
    db_session.commit()
    n = db_session.scalar(select(func.count(SourceDiscrepancy.id)).where(
        SourceDiscrepancy.stock_id == stock.id))
    assert n == 1


def test_agreement_resolves_then_redivergence_reopens(db_session):
    stock = make_stock(db_session, "RESOLVE")
    now = _now()
    R.record_discrepancy(db_session, stock.id, _price_comp(), now)
    db_session.commit()
    # sources now agree on the same key → resolve it.
    assert R.resolve_discrepancy(db_session, stock.id, "price", "2025-09-30", now) == 1
    db_session.commit()
    row = db_session.scalar(select(SourceDiscrepancy).where(
        SourceDiscrepancy.stock_id == stock.id))
    assert row.resolved_at is not None
    # a later re-divergence of the same key re-opens it and counts as NEW again.
    assert R.record_discrepancy(db_session, stock.id, _price_comp(), _now()) is True
    db_session.commit()
    db_session.refresh(row)
    assert row.resolved_at is None
    # resolving when nothing is open is a no-op.
    R.resolve_discrepancy(db_session, stock.id, "price", "2025-09-30", _now())
    db_session.commit()
    assert db_session.scalar(select(func.count(SourceDiscrepancy.id))) == 1


# ============================================================
# trust math — rolling 90d window over reconcile job_runs
# ============================================================

def _reconcile_run(session, *, started, tally, status="success"):
    session.add(JobRun(job_type="reconcile", target="active", status=status,
                       started_at=started, finished_at=started,
                       stats={"trust_tally": tally}))
    session.commit()


def test_trust_sums_window_and_excludes_older_than_90d(db_session):
    now = _now()
    # inside window: 10 comparisons, 9 agreements for bhavcopy|price.
    _reconcile_run(db_session, started=now - timedelta(days=10),
                   tally={"bhavcopy|price": {"c": 10, "a": 9}})
    # OUTSIDE the 90d window: must not count.
    _reconcile_run(db_session, started=now - timedelta(days=100),
                   tally={"bhavcopy|price": {"c": 100, "a": 0}})
    # errored run inside window: never counted.
    _reconcile_run(db_session, started=now - timedelta(days=5), status="error",
                   tally={"bhavcopy|price": {"c": 50, "a": 0}})

    written = R.update_trust(db_session, {"bhavcopy|price": {"c": 5, "a": 5}}, now)
    db_session.commit()
    assert written == 1
    row = db_session.scalar(select(SourceTrust).where(
        SourceTrust.source == "bhavcopy", SourceTrust.fact == "price"))
    assert (row.comparisons, row.agreements) == (15, 14)   # 10+5 / 9+5
    assert row.window_start <= now - timedelta(days=10)


def test_trust_upsert_is_idempotent_on_source_fact(db_session):
    now = _now()
    R.update_trust(db_session, {"nse|promoter_pct": {"c": 4, "a": 4}}, now)
    db_session.commit()
    R.update_trust(db_session, {"nse|promoter_pct": {"c": 6, "a": 3}}, now)
    db_session.commit()
    rows = db_session.scalars(select(SourceTrust).where(
        SourceTrust.source == "nse")).all()
    assert len(rows) == 1   # one row per (source, fact), overwritten not duplicated
    assert (rows[0].comparisons, rows[0].agreements) == (6, 3)


# ============================================================
# the reconcile job — end to end, counts, and the wholesale alert
# ============================================================

def test_reconcile_job_records_discrepancy_and_trust(db_session):
    good = make_stock(db_session, "OKAY")
    d = _days(-1).date()
    _yf(db_session, good, captured_at=_days(-1), last_date=d.isoformat(), last_close=100.0)
    _bar(db_session, good, d, 100.1)     # agrees

    bad = make_stock(db_session, "BADPX")
    _yf(db_session, bad, captured_at=_days(-1), last_date=d.isoformat(), last_close=100.0)
    _bar(db_session, bad, d, 120.0)      # diverges

    stats = R.reconcile(verbose=False)
    assert stats["compared"] == 2
    assert stats["agreements"] == 1
    assert stats["discrepancies"] == 1
    assert stats["new_discrepancies"] == 1

    open_rows = db_session.scalar(select(func.count(SourceDiscrepancy.id)).where(
        SourceDiscrepancy.resolved_at.is_(None)))
    assert open_rows == 1
    # both sides of the price fact got a trust row; the agree/compare tallies are symmetric.
    trust = {(t.source, t.fact): (t.agreements, t.comparisons)
             for t in db_session.scalars(select(SourceTrust)).all()}
    assert trust[("bhavcopy", "price")] == (1, 2)
    assert trust[("yfinance", "price")] == (1, 2)


def test_reconcile_pages_owner_on_wholesale_discrepancy_spike(db_session, monkeypatch):
    sent = []
    monkeypatch.setattr("engine.notify.notify_safe",
                        lambda *a, **k: sent.append((a, k)) or True)
    d = _days(-1).date()
    # One more than the threshold, every one diverging → a source went bad wholesale.
    for i in range(R.NEW_DISCREPANCY_ALERT + 1):
        s = make_stock(db_session, f"SPIKE{i}")
        _yf(db_session, s, captured_at=_days(-1), last_date=d.isoformat(), last_close=100.0)
        _bar(db_session, s, d, 130.0)

    stats = R.reconcile(verbose=False)
    assert stats["new_discrepancies"] == R.NEW_DISCREPANCY_ALERT + 1
    assert sent, "a wholesale discrepancy spike must page the owner"
    assert "spike" in sent[0][0][0].lower()


def test_reconcile_below_threshold_is_quiet(db_session, monkeypatch):
    sent = []
    monkeypatch.setattr("engine.notify.notify_safe",
                        lambda *a, **k: sent.append((a, k)) or True)
    d = _days(-1).date()
    s = make_stock(db_session, "ONEBAD")
    _yf(db_session, s, captured_at=_days(-1), last_date=d.isoformat(), last_close=100.0)
    _bar(db_session, s, d, 130.0)
    R.reconcile(verbose=False)
    assert sent == []   # one discrepancy is noise, not an alert


# ============================================================
# admin API contract
# ============================================================

@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def test_reconciliation_endpoint_contract(client, db_session):
    stock = make_stock(db_session, "APICON", company_name="Api Con Ltd")
    now = _now()
    db_session.add(SourceTrust(source="bhavcopy", fact="price",
                               agreements=9, comparisons=10, window_start=now,
                               computed_at=now))
    db_session.add(SourceDiscrepancy(
        stock_id=stock.id, fact="price", fact_key="2025-09-30",
        source_a="bhavcopy", value_a=120.0, source_b="yfinance", value_b=100.0,
        divergence_pct=16.7, detected_at=now, resolved_at=None))
    # a resolved one must NOT appear in the open list.
    db_session.add(SourceDiscrepancy(
        stock_id=stock.id, fact="market_cap", fact_key="2025-09-29",
        source_a="computed", value_a=1.0, source_b="yfinance", value_b=1.0,
        divergence_pct=0.0, detected_at=now, resolved_at=now))
    db_session.add(JobRun(job_type="reconcile", target="active", status="success",
                          started_at=now, finished_at=now,
                          stats={"stocks": 1, "compared": 1, "agreements": 0,
                                 "discrepancies": 1, "new_discrepancies": 1,
                                 "resolved": 0, "stale": 0, "missing": 0}))
    db_session.commit()

    r = client.get("/api/admin/reconciliation")
    assert r.status_code == 200
    body = r.json()
    assert body["compared"] == 1 and body["new_discrepancies"] == 1
    assert body["open_count"] == 1
    assert len(body["trust"]) == 1
    assert body["trust"][0]["agreement_rate"] == pytest.approx(0.9)
    assert len(body["open"]) == 1
    row = body["open"][0]
    assert row["symbol"] == "APICON" and row["fact"] == "price"
    assert row["source_a"] == "bhavcopy" and row["source_b"] == "yfinance"
