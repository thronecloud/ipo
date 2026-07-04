"""Daily OHLCV ingestion: append-only upsert + refresh_one wiring.

`fetch_payload` is monkeypatched so no network / yfinance call ever happens.
"""

from datetime import date

from sqlalchemy import func, select

from db.models import DailyPrice
from engine import repo
from engine.ingest import yf_refresh as yfr
from tests.factories import make_stock, yf_payload


def _bars(start_day: int, n: int) -> list[dict]:
    return [
        {
            "date": date(2026, 1, start_day + i),
            "open": 100.0 + i,
            "high": 105.0 + i,
            "low": 99.0 + i,
            "close": 102.0 + i,
            "volume": 1000 + i,
        }
        for i in range(n)
    ]


def test_upsert_inserts_only_new_dates(db_session):
    stock = make_stock(db_session, "PRICES1")

    inserted = repo.upsert_daily_prices(db_session, stock.id, _bars(1, 3))
    db_session.commit()
    assert inserted == 3
    assert db_session.scalar(
        select(func.count()).select_from(DailyPrice).where(DailyPrice.stock_id == stock.id)
    ) == 3

    # Overlapping range (2 old dates + 2 new): only the 2 new ones land.
    inserted2 = repo.upsert_daily_prices(db_session, stock.id, _bars(2, 4))
    db_session.commit()
    assert inserted2 == 2
    assert db_session.scalar(
        select(func.count()).select_from(DailyPrice).where(DailyPrice.stock_id == stock.id)
    ) == 5


def test_upsert_empty_is_noop(db_session):
    stock = make_stock(db_session, "PRICES2")
    assert repo.upsert_daily_prices(db_session, stock.id, []) == 0


def test_stored_bar_values_roundtrip(db_session):
    stock = make_stock(db_session, "PRICES3")
    repo.upsert_daily_prices(db_session, stock.id, _bars(10, 1))
    db_session.commit()
    bar = db_session.scalar(select(DailyPrice).where(DailyPrice.stock_id == stock.id))
    assert (bar.date, bar.open, bar.high, bar.low, bar.close, bar.volume) == (
        date(2026, 1, 10), 100.0, 105.0, 99.0, 102.0, 1000,
    )


def test_refresh_one_persists_price_rows(db_session, monkeypatch):
    """refresh_one pops _price_rows from the payload into daily_prices."""
    stock = make_stock(db_session, "PRICES4", status="active")
    payload = yf_payload()
    payload["_price_rows"] = _bars(1, 5)
    monkeypatch.setattr(yfr, "fetch_payload", lambda sym: (payload, "full"))

    res = yfr.refresh_one(db_session, stock)
    db_session.commit()

    assert res == "new_snapshot"
    assert db_session.scalar(
        select(func.count()).select_from(DailyPrice).where(DailyPrice.stock_id == stock.id)
    ) == 5


def test_refresh_one_without_price_rows_is_safe(db_session, monkeypatch):
    """Legacy payloads with no _price_rows key just store zero bars (no crash)."""
    stock = make_stock(db_session, "PRICES5", status="active")
    monkeypatch.setattr(yfr, "fetch_payload", lambda sym: (yf_payload(), "full"))

    res = yfr.refresh_one(db_session, stock)
    db_session.commit()

    assert res == "new_snapshot"
    assert db_session.scalar(
        select(func.count()).select_from(DailyPrice).where(DailyPrice.stock_id == stock.id)
    ) == 0
