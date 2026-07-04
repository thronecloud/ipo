"""Point-in-time event study: did the engine's scores actually pick winners?

Core honesty rules under test:
- No lookahead: entry price is the first close strictly AFTER information_date.
- Survivorship-safe: parked/dead stocks stay in the cohort; unpriced stocks are
  REPORTED as uncovered, never silently dropped.
- Excess return is vs the benchmark over the SAME window.
"""

from datetime import date, datetime, timedelta, timezone

from db.models import CompositeScoreHistory
from engine import repo
from engine.backtest.study import run_event_study, spearman
from tests.factories import make_stock


BENCH = "BSE-SMLCAP.BO"


def _bars(start: date, closes: list[float]) -> list[dict]:
    """One bar per weekday starting at `start` (skips Sat/Sun like a real market)."""
    rows, d, i = [], start, 0
    while i < len(closes):
        if d.weekday() < 5:
            c = closes[i]
            rows.append({"date": d, "open": c, "high": c, "low": c, "close": c,
                         "volume": 1000})
            i += 1
        d += timedelta(days=1)
    return rows


def _history_row(session, stock, *, info_date: date, composite: float,
                 lcb: float | None = None, tier: str = "high",
                 recommendation: str = "HOLD") -> CompositeScoreHistory:
    row = CompositeScoreHistory(
        stock_id=stock.id,
        as_of_date=info_date,
        information_date=datetime(info_date.year, info_date.month, info_date.day,
                                  12, 0, tzinfo=timezone.utc),
        composite_score=composite,
        lcb=lcb if lcb is not None else composite - 10,
        confidence_tier=tier,
        consensus_recommendation=recommendation,
        factor_version="axis-v1",
        analysis_coverage=10,
    )
    session.add(row)
    session.commit()
    return row


def _seed_benchmark(session, start: date, n: int = 250, base: float = 10000.0):
    # Flat benchmark: excess return == raw return, keeps assertions exact.
    repo.upsert_index_prices(session, BENCH, _bars(start, [base] * n))
    session.commit()


MON = date(2026, 1, 5)  # a Monday


def test_entry_is_first_close_strictly_after_information_date(db_session):
    stock = make_stock(db_session, "BT1")
    _history_row(db_session, stock, info_date=MON, composite=80.0)
    # Bars ON the info date and after: entry must skip the same-day bar.
    repo.upsert_daily_prices(db_session, stock.id,
                             _bars(MON, [100.0, 110.0, 121.0, 121.0, 121.0]))
    _seed_benchmark(db_session, MON)
    db_session.commit()

    report = run_event_study(db_session, benchmark=BENCH, horizons=(1, 2))
    (row,) = report["stocks"]
    assert row["entry_date"] == date(2026, 1, 6)
    assert row["entry_price"] == 110.0
    # 1 trading day later close=121 -> +10%; 2 days -> 121 again.
    assert abs(row["returns"][1] - 10.0) < 1e-9
    assert abs(row["returns"][2] - 10.0) < 1e-9


def test_excess_return_is_vs_benchmark_same_window(db_session):
    stock = make_stock(db_session, "BT2")
    _history_row(db_session, stock, info_date=MON, composite=80.0)
    repo.upsert_daily_prices(db_session, stock.id, _bars(MON, [100.0, 100.0, 110.0]))
    # Benchmark rises 100->104 over the same window (+4%).
    repo.upsert_index_prices(db_session, BENCH,
                             _bars(MON, [100.0, 100.0, 104.0, 104.0]))
    db_session.commit()

    report = run_event_study(db_session, benchmark=BENCH, horizons=(1,))
    (row,) = report["stocks"]
    assert abs(row["returns"][1] - 10.0) < 1e-9
    assert abs(row["excess"][1] - 6.0) < 1e-6  # 10% - 4%


def test_unpriced_stock_reported_not_dropped(db_session):
    priced = make_stock(db_session, "BT3")
    unpriced = make_stock(db_session, "BT4")
    _history_row(db_session, priced, info_date=MON, composite=70.0)
    _history_row(db_session, unpriced, info_date=MON, composite=90.0)
    repo.upsert_daily_prices(db_session, priced.id, _bars(MON, [100.0, 101.0]))
    _seed_benchmark(db_session, MON)

    report = run_event_study(db_session, benchmark=BENCH, horizons=(1,))
    assert report["cohort_size"] == 2
    assert report["priced"] == 1
    assert "BT4" in report["unpriced_symbols"]


def test_parked_stock_stays_in_cohort(db_session):
    stock = make_stock(db_session, "BT5", status="parked")
    _history_row(db_session, stock, info_date=MON, composite=60.0)
    repo.upsert_daily_prices(db_session, stock.id, _bars(MON, [100.0, 90.0]))
    _seed_benchmark(db_session, MON)

    report = run_event_study(db_session, benchmark=BENCH, horizons=(1,))
    assert report["cohort_size"] == 1
    assert report["stocks"][0]["symbol"] == "BT5"


def test_horizon_beyond_series_is_none(db_session):
    stock = make_stock(db_session, "BT6")
    _history_row(db_session, stock, info_date=MON, composite=75.0)
    repo.upsert_daily_prices(db_session, stock.id, _bars(MON, [100.0, 105.0, 106.0]))
    _seed_benchmark(db_session, MON)

    report = run_event_study(db_session, benchmark=BENCH, horizons=(1, 63))
    (row,) = report["stocks"]
    assert row["returns"][1] is not None
    assert row["returns"][63] is None


def test_tier_aggregation_and_hit_rate(db_session):
    # Two 'high' names: +10% and -10% vs flat benchmark -> hit rate 50%.
    for sym, last in (("BT7", 110.0), ("BT8", 90.0)):
        st = make_stock(db_session, sym)
        _history_row(db_session, st, info_date=MON, composite=80.0, tier="high")
        repo.upsert_daily_prices(db_session, st.id, _bars(MON, [100.0, 100.0, last]))
    _seed_benchmark(db_session, MON)

    report = run_event_study(db_session, benchmark=BENCH, horizons=(1,))
    tier = report["by_tier"]["high"]
    assert tier["n"] == 2 and tier["priced"] == 2
    assert abs(tier["mean_excess"][1] - 0.0) < 1e-9
    assert abs(tier["hit_rate"][1] - 50.0) < 1e-9


def test_ic_positive_when_score_orders_returns(db_session):
    # Higher composite -> higher forward return, perfectly monotone: IC = 1.
    for i, (sym, comp, last) in enumerate(
        (("BT9", 90.0, 130.0), ("BT10", 70.0, 110.0), ("BT11", 50.0, 90.0))
    ):
        st = make_stock(db_session, sym)
        _history_row(db_session, st, info_date=MON, composite=comp, lcb=comp - 5)
        repo.upsert_daily_prices(db_session, st.id, _bars(MON, [100.0, 100.0, last]))
    _seed_benchmark(db_session, MON)

    report = run_event_study(db_session, benchmark=BENCH, horizons=(1,))
    assert abs(report["ic"]["composite"][1] - 1.0) < 1e-9
    assert abs(report["ic"]["lcb"][1] - 1.0) < 1e-9


def test_api_backtest_endpoint(db_session):
    from fastapi.testclient import TestClient
    from api.main import app

    stock = make_stock(db_session, "BTAPI")
    _history_row(db_session, stock, info_date=MON, composite=80.0, tier="high")
    repo.upsert_daily_prices(db_session, stock.id, _bars(MON, [100.0, 100.0, 110.0]))
    _seed_benchmark(db_session, MON)

    with TestClient(app) as client:
        resp = client.get("/api/backtest", params={"benchmark": BENCH})
    assert resp.status_code == 200
    body = resp.json()
    assert body["cohort_size"] == 1
    assert body["benchmark"] == BENCH
    assert body["stocks"][0]["symbol"] == "BTAPI"
    assert body["by_tier"]["high"]["n"] == 1
    assert "ic" in body


def test_spearman_basics():
    assert spearman([1, 2, 3], [10, 20, 30]) == 1.0
    assert spearman([1, 2, 3], [30, 20, 10]) == -1.0
    assert spearman([1, 2], [1, 1]) is None          # zero variance
    assert spearman([1], [2]) is None                 # too short
