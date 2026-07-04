"""Benchmark index price series: IndexPrice ORM + append-only upsert + ingest.

The backtest's excess-return math is only as honest as its benchmark: without an
index series in the DB every "did we outperform?" answer is vibes. ^CNXSC (Nifty
Smallcap 250) is the certified comparator for this IPO/SME-heavy universe.

`refresh_index_prices` is tested with a monkeypatched fetcher — no network.
"""

from datetime import date

from sqlalchemy import func, select

from db.models import IndexPrice
from engine import repo
from engine.ingest import index_prices as ip


def _bars(start_day: int, n: int) -> list[dict]:
    return [
        {
            "date": date(2026, 2, start_day + i),
            "open": 15000.0 + i,
            "high": 15100.0 + i,
            "low": 14900.0 + i,
            "close": 15050.0 + i,
            "volume": 1_000_000 + i,
        }
        for i in range(n)
    ]


def test_upsert_inserts_only_new_dates(db_session):
    inserted = repo.upsert_index_prices(db_session, "^CNXSC", _bars(1, 3))
    db_session.commit()
    assert inserted == 3

    # Overlap (2 old + 2 new): append-only, only the new dates land.
    inserted2 = repo.upsert_index_prices(db_session, "^CNXSC", _bars(2, 4))
    db_session.commit()
    assert inserted2 == 2
    assert db_session.scalar(
        select(func.count()).select_from(IndexPrice).where(IndexPrice.symbol == "^CNXSC")
    ) == 5


def test_upsert_empty_is_noop(db_session):
    assert repo.upsert_index_prices(db_session, "^CNXSC", []) == 0


def test_same_date_different_symbol_both_kept(db_session):
    repo.upsert_index_prices(db_session, "^CNXSC", _bars(1, 1))
    repo.upsert_index_prices(db_session, "^NSEI", _bars(1, 1))
    db_session.commit()
    assert db_session.scalar(select(func.count()).select_from(IndexPrice)) == 2


def test_stored_bar_values_roundtrip(db_session):
    repo.upsert_index_prices(db_session, "^CNXSC", _bars(10, 1))
    db_session.commit()
    bar = db_session.scalar(select(IndexPrice).where(IndexPrice.symbol == "^CNXSC"))
    assert (bar.symbol, bar.date, bar.open, bar.high, bar.low, bar.close, bar.volume) == (
        "^CNXSC", date(2026, 2, 10), 15000.0, 15100.0, 14900.0, 15050.0, 1_000_000,
    )


def test_refresh_index_prices_persists_rows(db_session, monkeypatch):
    """refresh_index_prices pulls each benchmark's history and appends bars."""
    monkeypatch.setattr(ip, "_fetch_history_rows", lambda symbol: _bars(1, 4))

    stats = ip.refresh_index_prices(symbols=["^CNXSC"])

    assert stats["bars_added"] == 4
    assert db_session.scalar(
        select(func.count()).select_from(IndexPrice).where(IndexPrice.symbol == "^CNXSC")
    ) == 4


def test_refresh_index_prices_defaults_to_benchmarks(db_session, monkeypatch):
    """No symbols arg -> every configured benchmark is refreshed."""
    seen = []

    def fake(symbol):
        seen.append(symbol)
        return _bars(1, 2)

    monkeypatch.setattr(ip, "_fetch_history_rows", fake)
    stats = ip.refresh_index_prices()

    assert seen == list(ip.BENCHMARKS)
    assert stats["bars_added"] == 2 * len(ip.BENCHMARKS)


def test_refresh_index_prices_empty_history_counted_not_fatal(db_session, monkeypatch):
    """A benchmark with no usable history is counted and skipped, not a crash."""
    monkeypatch.setattr(ip, "_fetch_history_rows", lambda symbol: [])
    stats = ip.refresh_index_prices(symbols=["^CNXSC"])
    assert stats["no_history"] == 1
    assert stats["bars_added"] == 0
