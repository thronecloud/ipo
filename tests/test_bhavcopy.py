"""Bhavcopy ingestion — UDiFF parse, NSE/BSE symbol+ISIN resolution, corrective
upsert wiring, not-published (404) handling, previous-day fallback, idempotency.

The `_fetch_*` network seams are monkeypatched; nothing here hits the network."""

import io
import zipfile
from datetime import date
from pathlib import Path

from sqlalchemy import func, select

from db.models import DailyPrice, JobRun
from engine.ingest import bhavcopy as bc
from factories import make_stock

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _csv(name: str) -> str:
    return (FIXTURES / name).read_text()


def _zip_of(csv_text: str, member="BhavCopy_NSE_CM_0_0_0_20260720_F_0000.csv") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(member, csv_text)
    return buf.getvalue()


def _seed_universe(session):
    """ATHERENERG (matches NSE by ticker, BSE by ISIN) and TESTCO2 (matches NSE by
    ticker, BSE by ISIN-fallback-to-ticker)."""
    make_stock(session, "ATHERENERG", nse_symbol="ATHERENERG", isin="INE12345A01011")
    make_stock(session, "TESTCO2", isin=None)


# ---------- parsing ----------

def test_parse_udiff_extracts_equity_bars_only():
    bars = bc.parse_udiff(_csv("nse_bhavcopy.csv"))
    # 3 STK equity rows; the IDX row is filtered out.
    assert len(bars) == 3
    ather = next(b for b in bars if b["symbol"] == "ATHERENERG")
    assert ather["date"] == date(2026, 7, 20)
    assert (ather["open"], ather["high"], ather["low"], ather["close"]) == (
        100.0, 105.0, 99.0, 102.5)
    assert ather["volume"] == 150000
    assert ather["isin"] == "INE12345A01011"
    assert ather["series"] == "EQ"
    assert "NIFTY 50" not in {b["symbol"] for b in bars}


def test_unzip_single_csv_roundtrips():
    text = _csv("nse_bhavcopy.csv")
    assert bc._unzip_single_csv(_zip_of(text)) == text


# ---------- resolution + landing ----------

def test_nse_rows_resolve_by_symbol_and_land(db_session):
    _seed_universe(db_session)
    counts = {}
    priced = bc.ingest_bars(db_session, "NSE", bc.parse_udiff(_csv("nse_bhavcopy.csv")),
                            counts)
    db_session.commit()
    assert len(priced) == 2                       # ATHERENERG + TESTCO2
    assert counts["unmatched"] == 1               # NONUNIV
    assert counts["bars_added"] == 2
    stored = db_session.scalars(select(DailyPrice)).all()
    assert {round(b.close, 2) for b in stored} == {102.5, 205.0}


def test_bse_rows_resolve_by_isin_then_symbol(db_session):
    _seed_universe(db_session)
    counts = {}
    # BSE row 1: ticker ATHRSCRIP (no match) but ISIN INE12345A01011 → resolves ATHERENERG.
    # BSE row 2: ISIN INE00000X00000 (no match) but ticker TESTCO2 → symbol fallback.
    # BSE row 3: BSEONLY / unknown ISIN → unmatched.
    priced = bc.ingest_bars(db_session, "BSE", bc.parse_udiff(_csv("bse_bhavcopy.csv")),
                            counts)
    db_session.commit()
    assert len(priced) == 2
    assert counts["unmatched"] == 1
    # ATHERENERG resolved via ISIN carries BSE's close (103.0); TESTCO2 via ticker (206.0).
    closes = {round(b.close, 2) for b in db_session.scalars(select(DailyPrice)).all()}
    assert closes == {103.0, 206.0}


def test_bars_land_via_corrective_upsert_and_tally_rebase(db_session):
    _seed_universe(db_session)
    counts = {}
    bc.ingest_bars(db_session, "NSE", bc.parse_udiff(_csv("nse_bhavcopy.csv")), counts)
    db_session.commit()
    assert counts.get("bars_rebased", 0) == 0

    # Re-ingest the same day with a corporate-action-rebased close: no new bar, the
    # existing one is rewritten and reported under bars_rebased.
    bars = bc.parse_udiff(_csv("nse_bhavcopy.csv"))
    for b in bars:
        b["close"] = b["close"] / 10
    counts2 = {}
    priced = bc.ingest_bars(db_session, "NSE", bars, counts2)
    db_session.commit()
    assert counts2["bars_added"] == 0
    assert counts2["bars_rebased"] == 2           # both matched bars rewritten
    ather = db_session.scalar(select(DailyPrice).where(
        DailyPrice.close < 20))
    assert round(ather.close, 3) == 10.25


def test_idempotent_reingest_adds_zero_bars(db_session):
    _seed_universe(db_session)
    bars = bc.parse_udiff(_csv("nse_bhavcopy.csv"))
    counts = {}
    assert sum(1 for _ in bc.ingest_bars(db_session, "NSE", bars, counts)) == 2
    db_session.commit()
    assert counts["bars_added"] == 2

    counts2 = {}
    bc.ingest_bars(db_session, "NSE", bc.parse_udiff(_csv("nse_bhavcopy.csv")), counts2)
    db_session.commit()
    assert counts2["bars_added"] == 0
    assert counts2.get("bars_rebased", 0) == 0
    assert db_session.scalar(select(func.count()).select_from(DailyPrice)) == 2


# ---------- trading-day selection ----------

def test_weekend_target_rolls_back_to_friday():
    saturday = date(2026, 7, 18)
    cands = bc._trading_candidates(saturday, last_ingested=None)
    assert cands[0] == date(2026, 7, 17)          # Friday
    assert cands[1] == date(2026, 7, 16)          # Thursday (fallback)


def test_previous_day_fallback_only_when_not_already_ingested():
    tuesday = date(2026, 7, 21)
    # Nothing ingested yet → today + previous trading day.
    assert bc._trading_candidates(tuesday, None) == [date(2026, 7, 21), date(2026, 7, 20)]
    # Monday already ingested → only today is a candidate (no redundant re-fetch).
    assert bc._trading_candidates(tuesday, date(2026, 7, 20)) == [date(2026, 7, 21)]


# ---------- end-to-end job ----------

def test_fetch_bhavcopy_ingests_both_feeds_and_records_job_run(db_session, monkeypatch):
    _seed_universe(db_session)
    monkeypatch.setattr(bc, "_fetch_nse_zip", lambda d: _zip_of(_csv("nse_bhavcopy.csv")))
    monkeypatch.setattr(bc, "_fetch_bse_csv", lambda d: _csv("bse_bhavcopy.csv"))

    stats = bc.fetch_bhavcopy(on_date=date(2026, 7, 20), verbose=False)
    assert stats["matched_stocks"] == 2           # union across both feeds
    assert stats["not_published"] == 0
    assert stats["date"] == "2026-07-20"
    # unmatched: NONUNIV (NSE) + BSEONLY (BSE)
    assert stats["unmatched"] == 2

    job = db_session.scalar(
        select(JobRun).where(JobRun.job_type == "bhavcopy").order_by(JobRun.id.desc()))
    assert job.status == "success"
    assert job.stats["matched_stocks"] == 2


def test_not_published_404_is_counted_not_error(db_session, monkeypatch):
    _seed_universe(db_session)
    monkeypatch.setattr(bc, "_fetch_nse_zip", lambda d: None)   # 404 → not published
    monkeypatch.setattr(bc, "_fetch_bse_csv", lambda d: None)

    stats = bc.fetch_bhavcopy(on_date=date(2026, 7, 20), verbose=False)
    assert stats["not_published"] == 2
    assert stats["bars_added"] == 0
    assert "date" not in stats                     # nothing ingested
    job = db_session.scalar(
        select(JobRun).where(JobRun.job_type == "bhavcopy").order_by(JobRun.id.desc()))
    assert job.status == "success"                 # a holiday is not a failure


def test_today_absent_falls_back_to_previous_trading_day(db_session, monkeypatch):
    _seed_universe(db_session)
    # Today (Tue 21st) not published; the previous trading day (Mon 20th) is.
    def load(exchange, on_date):
        if on_date == date(2026, 7, 20):
            return _csv("nse_bhavcopy.csv") if exchange == "NSE" else _csv("bse_bhavcopy.csv")
        return None
    monkeypatch.setattr(bc, "_load_csv", load)

    stats = bc.fetch_bhavcopy(on_date=date(2026, 7, 21), verbose=False)
    assert stats["date"] == "2026-07-20"
    assert stats["matched_stocks"] == 2


def test_universe_completeness_counts_stocks_without_a_bar(db_session, monkeypatch):
    _seed_universe(db_session)
    make_stock(db_session, "DELISTED", nse_symbol="DELISTED")   # never in the file
    monkeypatch.setattr(bc, "_fetch_nse_zip", lambda d: _zip_of(_csv("nse_bhavcopy.csv")))
    monkeypatch.setattr(bc, "_fetch_bse_csv", lambda d: _csv("bse_bhavcopy.csv"))

    stats = bc.fetch_bhavcopy(on_date=date(2026, 7, 20), verbose=False)
    assert stats["universe_missing"] == 1          # DELISTED got no bar
