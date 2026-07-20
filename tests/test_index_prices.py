"""Benchmark index price series: IndexPrice ORM + corrective upsert + ingest.

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
    # Values derive from the date, not the position in the window, so a bar has the
    # same values whichever fetch window contains it. Position-indexed values would
    # make two overlapping windows disagree about one date, and the corrective
    # upsert would faithfully rewrite history to whichever arrived last.
    return [
        {
            "date": date(2026, 2, day),
            "open": 15000.0 + (day - 1),
            "high": 15100.0 + (day - 1),
            "low": 14900.0 + (day - 1),
            "close": 15050.0 + (day - 1),
            "volume": 1_000_000 + (day - 1),
        }
        for day in range(start_day, start_day + n)
    ]


def test_upsert_inserts_only_new_dates(db_session):
    inserted = repo.upsert_index_prices(db_session, "^CNXSC", _bars(1, 3))
    db_session.commit()
    assert inserted == 3

    # Overlap (2 old + 2 new): the unchanged bars are skipped, only the new dates land.
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
        "^CNXSC", date(2026, 2, 10), 15009.0, 15109.0, 14909.0, 15059.0, 1_000_009,
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


def test_bad_index_bar_is_repaired_by_reingest(db_session):
    """Index bars feed every excess-return number. A bar stored with a NULL close is
    a hole in that denominator, and append-only storage makes it permanent."""
    repo.upsert_index_prices(db_session, "^NSEI", [
        {"date": date(2026, 2, 1), "open": None, "high": None,
         "low": None, "close": None, "volume": None},
    ])
    db_session.commit()

    # The hole is rewritten in place, not inserted, so the return is 0 and the repair
    # is reported as a rebase.
    counts: dict = {}
    assert repo.upsert_index_prices(db_session, "^NSEI", _bars(1, 1), counts=counts) == 0
    db_session.commit()
    assert counts["bars_rebased"] == 1
    bar = db_session.scalar(select(IndexPrice).where(IndexPrice.symbol == "^NSEI"))
    assert bar.close == 15050.0


def test_all_null_incoming_does_not_degrade_stored_index_bar(db_session):
    """A glitched index fetch row (all fields None) must not blank a good stored bar.
    No data means no-op: nothing inserted, nothing rebased, stored values intact."""
    repo.upsert_index_prices(db_session, "^NSEI", _bars(1, 1))
    db_session.commit()

    counts: dict = {}
    inserted = repo.upsert_index_prices(db_session, "^NSEI", [
        {"date": date(2026, 2, 1), "open": None, "high": None,
         "low": None, "close": None, "volume": None},
    ], counts=counts)
    db_session.commit()
    assert inserted == 0
    assert counts.get("bars_rebased", 0) == 0
    bar = db_session.scalar(select(IndexPrice).where(IndexPrice.symbol == "^NSEI"))
    assert (bar.open, bar.high, bar.low, bar.close, bar.volume) == (
        15000.0, 15100.0, 14900.0, 15050.0, 1_000_000)


def test_partial_index_row_corrects_carried_field_and_preserves_rest(db_session):
    """A partial index row corrects the field it carries (close) and preserves the
    stored open that arrives as NULL."""
    repo.upsert_index_prices(db_session, "^NSEI", _bars(1, 1))  # open=15000, close=15050
    db_session.commit()

    counts: dict = {}
    inserted = repo.upsert_index_prices(db_session, "^NSEI", [
        {"date": date(2026, 2, 1), "open": None, "high": 15100.0,
         "low": 14900.0, "close": 15075.0, "volume": 1_000_000},
    ], counts=counts)
    db_session.commit()
    assert inserted == 0
    assert counts["bars_rebased"] == 1
    bar = db_session.scalar(select(IndexPrice).where(IndexPrice.symbol == "^NSEI"))
    assert bar.open == 15000.0    # preserved
    assert bar.close == 15075.0   # corrected


def test_duplicate_date_payload_stores_last_row(db_session):
    """A benchmark frame with the same date twice must not abort the ingest with a
    CardinalityViolation. The payload is deduped and the LAST row wins."""
    inserted = repo.upsert_index_prices(db_session, "^NSEI", [
        {"date": date(2026, 2, 1), "open": 15000.0, "high": 15100.0,
         "low": 14900.0, "close": 15050.0, "volume": 1_000_000},
        {"date": date(2026, 2, 1), "open": 16000.0, "high": 16100.0,
         "low": 15900.0, "close": 16050.0, "volume": 2_000_000},
    ])
    db_session.commit()
    assert inserted == 1
    bar = db_session.scalar(select(IndexPrice).where(IndexPrice.symbol == "^NSEI"))
    assert (bar.open, bar.high, bar.low, bar.close, bar.volume) == (
        16000.0, 16100.0, 15900.0, 16050.0, 2_000_000)


def test_unchanged_index_bars_are_not_rewritten(db_session):
    """`period=max` re-sends the whole series nightly; identical bars must not churn."""
    rows = _bars(1, 5)
    counts: dict = {}
    assert repo.upsert_index_prices(db_session, "^NSEI", rows) == 5
    db_session.commit()
    # 0 inserted AND 0 rewritten — the rebase tally is what proves the identical bars
    # were suppressed rather than silently rewritten.
    assert repo.upsert_index_prices(db_session, "^NSEI", rows, counts=counts) == 0
    assert counts.get("bars_rebased", 0) == 0


def test_index_refresh_records_corrected_bars_in_job_stats(db_session, monkeypatch):
    """A provider correction to an index bar is a rewrite of research input, and gets
    the same durable record as a stock rebase."""
    from db.models import JobRun
    repo.upsert_index_prices(db_session, "^CNXSC", _bars(1, 2))
    db_session.commit()

    corrected = [dict(b, close=b["close"] + 25.0) for b in _bars(1, 2)]
    monkeypatch.setattr(ip, "_fetch_history_rows", lambda symbol: corrected)
    stats = ip.refresh_index_prices(symbols=["^CNXSC"])

    assert stats["bars_rebased"] == 2
    job = db_session.scalar(
        select(JobRun).where(JobRun.job_type == "index_prices")
        .order_by(JobRun.id.desc()).limit(1)
    )
    assert job.stats["bars_rebased"] == 2
