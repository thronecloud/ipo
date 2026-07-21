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
from engine.backtest.study import run_event_study, run_persona_study, spearman
from tests.factories import make_analysis, make_stock


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


RISING = [100.0, 100.0, 102.0, 104.0, 106.0, 108.0, 110.0, 112.0]  # entry idx1, reaches horizon 5


def test_event_study_ci_flags_true_effect(db_session):
    # Six names, every one +10% over a flat benchmark: the mean-excess CI must
    # clear zero (verdict positive) and the hit rate must clear the coin flip.
    for i in range(6):
        st = make_stock(db_session, f"CIP{i}")
        _history_row(db_session, st, info_date=MON, composite=70.0 + i)
        repo.upsert_daily_prices(db_session, st.id, _bars(MON, [100.0, 100.0, 110.0]))
    _seed_benchmark(db_session, MON)
    db_session.commit()

    report = run_event_study(db_session, benchmark=BENCH, horizons=(1,), n_boot=500)
    ci = report["overall"]["mean_excess_ci"][1]
    assert ci is not None and ci["ci_low"] > 0 and ci["verdict"] == "positive"
    assert report["overall"]["hit_rate_ci"][1]["verdict"] == "positive"
    # Zero-variance returns leave the IC undefined, but the CI key still exists.
    assert 1 in report["ic_ci"]["composite"]


def test_event_study_ci_straddles_zero_on_noise(db_session):
    # Three winners (+10%) and three losers (-10%): mean excess ~ 0, so the CI
    # must straddle zero and read as indistinguishable.
    for i in range(6):
        last = 110.0 if i < 3 else 90.0
        st = make_stock(db_session, f"CIN{i}")
        _history_row(db_session, st, info_date=MON, composite=60.0 + i)
        repo.upsert_daily_prices(db_session, st.id, _bars(MON, [100.0, 100.0, last]))
    _seed_benchmark(db_session, MON)
    db_session.commit()

    report = run_event_study(db_session, benchmark=BENCH, horizons=(1,), n_boot=1000)
    ci = report["overall"]["mean_excess_ci"][1]
    assert ci["ci_low"] < 0 < ci["ci_high"]
    assert ci["verdict"] == "indistinguishable from zero"


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


# ── per-persona study ─────────────────────────────────────────────

MON_NOON = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)  # == the cohort cutoff


def _verdict(session, stock, persona, score, rec, *, at=None):
    """A persona's verdict on a stock, dated on/before the view formation."""
    make_analysis(session, stock, persona, score, recommendation=rec,
                  analyzed_at=at or MON_NOON)


def _persona(report, slug):
    return next(p for p in report["personas"] if p["persona"] == slug)


def test_persona_curve_rebased_and_forward_return(db_session):
    # Buffett BUYs a +10% winner; Munger AVOIDs it. Curve rebases to 100 at entry.
    st = make_stock(db_session, "PP1")
    _history_row(db_session, st, info_date=MON, composite=80.0)
    repo.upsert_daily_prices(db_session, st.id, _bars(MON, [100.0, 100.0, 110.0]))
    _seed_benchmark(db_session, MON)
    _verdict(db_session, st, "warren_buffett", 9, "BUY")
    _verdict(db_session, st, "charlie_munger", 2, "AVOID")
    db_session.commit()

    report = run_persona_study(db_session, benchmark=BENCH, horizons=(1,))
    assert report["curve_offsets"] == [0, 1]
    buffett = _persona(report, "warren_buffett")
    assert buffett["n_buy"] == 1 and buffett["n_avoid"] == 0
    # A single pick can't support a band — p5/p95 are honestly None, not the point.
    assert buffett["curve"] == [
        {"t": 0, "portfolio": 100.0, "benchmark": 100.0, "n": 1, "p5": None, "p95": None},
        {"t": 1, "portfolio": 110.0, "benchmark": 100.0, "n": 1, "p5": None, "p95": None},
    ]
    assert abs(buffett["stats"]["mean_return"][1] - 10.0) < 1e-9
    assert abs(buffett["stats"]["hit_rate"][1] - 100.0) < 1e-9

    munger = _persona(report, "charlie_munger")
    assert munger["n_buy"] == 0 and munger["n_avoid"] == 1 and munger["curve"] == []


def test_persona_buy_minus_avoid_spread(db_session):
    # Buffett BUYs a +10% winner and AVOIDs a -10% loser (flat benchmark, so
    # excess == raw). Spread = mean_excess(BUY) - mean_excess(AVOID) = +20.
    win = make_stock(db_session, "PPW")
    lose = make_stock(db_session, "PPL")
    _history_row(db_session, win, info_date=MON, composite=80.0)
    _history_row(db_session, lose, info_date=MON, composite=40.0)
    repo.upsert_daily_prices(db_session, win.id, _bars(MON, [100.0, 100.0, 110.0]))
    repo.upsert_daily_prices(db_session, lose.id, _bars(MON, [100.0, 100.0, 90.0]))
    _seed_benchmark(db_session, MON)
    _verdict(db_session, win, "warren_buffett", 9, "BUY")
    _verdict(db_session, lose, "warren_buffett", 2, "AVOID")
    db_session.commit()

    report = run_persona_study(db_session, benchmark=BENCH, horizons=(1,))
    buffett = _persona(report, "warren_buffett")
    assert buffett["n_buy"] == 1 and buffett["n_avoid"] == 1
    assert abs(buffett["spread"][1] - 20.0) < 1e-6


def test_subset_consensus_matches_single_persona(db_session):
    # Two personas both BUY the winner. The subset over one persona must equal
    # that persona's individual result (compute.ts parity, engine-side).
    st = make_stock(db_session, "PPS")
    _history_row(db_session, st, info_date=MON, composite=80.0)
    repo.upsert_daily_prices(db_session, st.id, _bars(MON, [100.0, 100.0, 110.0]))
    _seed_benchmark(db_session, MON)
    _verdict(db_session, st, "warren_buffett", 9, "BUY")
    _verdict(db_session, st, "charlie_munger", 8, "BUY")
    db_session.commit()

    report = run_persona_study(db_session, benchmark=BENCH, horizons=(1,),
                               personas=["warren_buffett"])
    buffett = _persona(report, "warren_buffett")
    assert report["subset"]["personas"] == ["warren_buffett"]
    assert report["subset"]["curve"] == buffett["curve"]
    assert report["subset"]["n_buy"] == buffett["n_buy"] == 1

    # A two-persona subset: both BUY -> consensus BUY -> the stock is a pick.
    both = run_persona_study(db_session, benchmark=BENCH, horizons=(1,),
                             personas=["warren_buffett", "charlie_munger"])
    assert both["subset"]["personas"] == ["warren_buffett", "charlie_munger"]
    assert both["subset"]["n_buy"] == 1


def test_persona_verdict_after_information_date_is_excluded(db_session):
    # A verdict dated AFTER the view formed must not leak in (no lookahead).
    st = make_stock(db_session, "PPX")
    _history_row(db_session, st, info_date=MON, composite=80.0)
    repo.upsert_daily_prices(db_session, st.id, _bars(MON, [100.0, 100.0, 110.0]))
    _seed_benchmark(db_session, MON)
    _verdict(db_session, st, "warren_buffett", 9, "BUY",
             at=datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc))  # months later
    db_session.commit()

    report = run_persona_study(db_session, benchmark=BENCH, horizons=(1,))
    buffett = _persona(report, "warren_buffett")
    assert buffett["n_buy"] == 0 and buffett["curve"] == []


def test_api_backtest_personas_endpoint(db_session):
    from fastapi.testclient import TestClient
    from api.main import app

    st = make_stock(db_session, "PPAPI")
    _history_row(db_session, st, info_date=MON, composite=80.0)
    repo.upsert_daily_prices(db_session, st.id, _bars(MON, [100.0, 100.0, 110.0]))
    _seed_benchmark(db_session, MON)
    _verdict(db_session, st, "warren_buffett", 9, "BUY")
    db_session.commit()

    with TestClient(app) as client:
        resp = client.get("/api/backtest/personas", params={"benchmark": BENCH})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["personas"]) == 10
    assert body["council"][0] == "warren_buffett"
    buffett = next(p for p in body["personas"] if p["persona"] == "warren_buffett")
    assert set(buffett) >= {"persona", "n_buy", "n_avoid", "stats", "spread", "curve"}
    assert buffett["n_buy"] == 1
    assert body["subset"]["personas"] == body["council"]  # default = full council
    assert len(body["council"]) == 10

    # personas= narrows the subset consensus.
    with TestClient(app) as client:
        one = client.get("/api/backtest/personas",
                         params={"benchmark": BENCH, "personas": "warren_buffett"})
    assert one.status_code == 200
    assert one.json()["subset"]["personas"] == ["warren_buffett"]

    # unknown slug is a 400, not a silent drop.
    with TestClient(app) as client:
        bad = client.get("/api/backtest/personas", params={"personas": "nobody"})
    assert bad.status_code == 400


def test_api_backtest_ci_fields(db_session):
    from fastapi.testclient import TestClient
    from api.main import app

    for i in range(4):
        st = make_stock(db_session, f"APIC{i}")
        _history_row(db_session, st, info_date=MON, composite=70.0 + i, tier="high")
        repo.upsert_daily_prices(db_session, st.id, _bars(MON, RISING))
    _seed_benchmark(db_session, MON)
    db_session.commit()

    with TestClient(app) as client:
        resp = client.get("/api/backtest", params={"benchmark": BENCH})
    assert resp.status_code == 200
    body = resp.json()
    # Horizon-keyed dicts arrive JSON-stringified: {"5": …}.
    assert "ic_ci" in body and "composite" in body["ic_ci"]
    ov = body["overall"]
    assert "mean_excess_ci" in ov and "hit_rate_ci" in ov
    ci = ov["mean_excess_ci"]["5"]
    assert ci is not None
    assert set(ci) >= {"point", "ci_low", "ci_high", "verdict", "n"}
    assert ci["verdict"] == "positive"  # four rising names beat a flat benchmark
    assert body["by_tier"]["high"]["mean_excess_ci"]["5"] is not None


# ── cohort completeness + delisting policy ────────────────────────

# Weekdays before MON=2026-01-05, so every bar is on/before the view forms.
PRE_VIEW_START = date(2025, 12, 29)


def test_cohort_accounting_partitions_stocks(db_session):
    # One stock of each kind, so the four-way partition is exact.
    measured = make_stock(db_session, "COM1")
    _history_row(db_session, measured, info_date=MON, composite=70.0)
    repo.upsert_daily_prices(db_session, measured.id, _bars(MON, RISING))

    no_bars = make_stock(db_session, "COM2")  # no daily prices at all
    _history_row(db_session, no_bars, info_date=MON, composite=60.0)

    predate = make_stock(db_session, "COM3")
    _history_row(db_session, predate, info_date=MON, composite=50.0)
    repo.upsert_daily_prices(db_session, predate.id,
                             _bars(PRE_VIEW_START, [100.0, 100.0, 100.0]))

    insufficient = make_stock(db_session, "COM4")
    _history_row(db_session, insufficient, info_date=MON, composite=55.0)
    # Entry exists (bar after MON) but never reaches the 5-day horizon.
    repo.upsert_daily_prices(db_session, insufficient.id,
                             _bars(MON, [100.0, 100.0, 110.0, 120.0]))
    _seed_benchmark(db_session, MON)
    db_session.commit()

    report = run_event_study(db_session, benchmark=BENCH, horizons=(5,))
    c = report["cohort"]
    assert c["scored"] == 4
    assert c["priceable"] == 3          # everyone but the no-bars name
    assert c["measured"] == 1
    assert c["excluded"] == {
        "no_bars": 1, "bars_predate_view": 1, "insufficient_forward": 1,
    }
    assert c["excluded_symbols"]["no_bars"] == ["COM2"]
    assert c["excluded_symbols"]["bars_predate_view"] == ["COM3"]
    assert c["excluded_symbols"]["insufficient_forward"] == ["COM4"]


def test_presumed_outcome_from_status(db_session):
    # No-bars stocks classified by status: fetch-dead -> delisted, merge ->
    # merged, a live-but-ungathered name -> unknown (not assumed dead).
    for sym, status in (("PO1", "unfetchable"), ("PO2", "stale"),
                        ("PO3", "listed_merged"), ("PO4", "active")):
        st = make_stock(db_session, sym, status=status)
        _history_row(db_session, st, info_date=MON, composite=60.0)
    _seed_benchmark(db_session, MON)
    db_session.commit()

    report = run_event_study(db_session, benchmark=BENCH, horizons=(5,))
    p = report["cohort"]["presumed_outcomes"]
    assert p == {"delisted": 2, "merged": 1, "unknown": 1}
    assert report["cohort"]["presumed_symbols"]["merged"] == ["PO3"]


def test_delisting_policy_constant_and_actual_last(db_session):
    # A no-bars delisted name gets the documented -50% constant; a delisted name
    # that DID trade after the view uses its actual last-price return instead.
    dead = make_stock(db_session, "DP1", status="unfetchable")  # no bars
    _history_row(db_session, dead, info_date=MON, composite=60.0)

    traded = make_stock(db_session, "DP2", status="stale")
    _history_row(db_session, traded, info_date=MON, composite=55.0)
    # Entry 100 -> last 120 (+20%), but never reaches the 5-day horizon.
    repo.upsert_daily_prices(db_session, traded.id,
                             _bars(MON, [100.0, 100.0, 110.0, 120.0]))
    _seed_benchmark(db_session, MON)
    db_session.commit()

    report = run_event_study(db_session, benchmark=BENCH, horizons=(5,))
    pol = report["cohort"]["policy"]
    assert report["delisting_return_policy"] == -50.0
    assert pol["delisting_return"] == -50.0
    assert pol["applied_constant"] == 1 and pol["symbols"]["constant"] == ["DP1"]
    assert pol["applied_actual_last"] == 1 and pol["symbols"]["actual_last"] == ["DP2"]


def test_policy_both_ways_diverge(db_session):
    # One measured winner (+10%) plus two vanished names folded back via policy
    # (-50% constant, +20% actual). Measured-only overall != policy-augmented.
    win = make_stock(db_session, "BW1")
    _history_row(db_session, win, info_date=MON, composite=70.0)
    repo.upsert_daily_prices(db_session, win.id, _bars(MON, RISING))  # +10% @ h=5

    dead = make_stock(db_session, "BW2", status="unfetchable")  # -> -50 constant
    _history_row(db_session, dead, info_date=MON, composite=60.0)

    traded = make_stock(db_session, "BW3", status="stale")  # -> +20 actual
    _history_row(db_session, traded, info_date=MON, composite=55.0)
    repo.upsert_daily_prices(db_session, traded.id,
                             _bars(MON, [100.0, 100.0, 110.0, 120.0]))
    _seed_benchmark(db_session, MON)
    db_session.commit()

    report = run_event_study(db_session, benchmark=BENCH, horizons=(5,))
    measured_only = report["overall"]["mean_excess"][5]
    with_policy = report["overall_with_policy"]["mean_excess"][5]
    assert abs(measured_only - 10.0) < 1e-9          # only the winner is measured
    # (10 - 50 + 20) / 3 = -6.667
    assert abs(with_policy - (-20.0 / 3.0)) < 1e-6
    assert measured_only != with_policy


def test_overall_with_policy_none_when_no_policy_applied(db_session):
    # No delisted exclusions -> nothing to fold in -> the with-policy stat is
    # honestly None, not a copy of the measured one.
    st = make_stock(db_session, "NP1")
    _history_row(db_session, st, info_date=MON, composite=70.0)
    repo.upsert_daily_prices(db_session, st.id, _bars(MON, RISING))
    _seed_benchmark(db_session, MON)
    db_session.commit()

    report = run_event_study(db_session, benchmark=BENCH, horizons=(5,))
    assert report["overall_with_policy"] is None
    assert report["cohort"]["policy"]["applied_constant"] == 0
    assert report["cohort"]["policy"]["applied_actual_last"] == 0


def test_persona_study_reports_cohort(db_session):
    st = make_stock(db_session, "PC1")
    _history_row(db_session, st, info_date=MON, composite=80.0)
    repo.upsert_daily_prices(db_session, st.id, _bars(MON, RISING))
    dead = make_stock(db_session, "PC2", status="unfetchable")
    _history_row(db_session, dead, info_date=MON, composite=60.0)
    _seed_benchmark(db_session, MON)
    _verdict(db_session, st, "warren_buffett", 9, "BUY")
    db_session.commit()

    report = run_persona_study(db_session, benchmark=BENCH, horizons=(5,))
    c = report["cohort"]
    assert c["scored"] == 2 and c["measured"] == 1
    assert c["excluded"]["no_bars"] == 1
    assert c["presumed_outcomes"]["delisted"] == 1
    assert report["delisting_return_policy"] == -50.0


def test_api_backtest_cohort_contract(db_session):
    from fastapi.testclient import TestClient
    from api.main import app

    win = make_stock(db_session, "ACC1")
    _history_row(db_session, win, info_date=MON, composite=70.0)
    repo.upsert_daily_prices(db_session, win.id, _bars(MON, RISING))
    dead = make_stock(db_session, "ACC2", status="unfetchable")
    _history_row(db_session, dead, info_date=MON, composite=60.0)
    _seed_benchmark(db_session, MON)
    db_session.commit()

    with TestClient(app) as client:
        resp = client.get("/api/backtest", params={"benchmark": BENCH})
    assert resp.status_code == 200
    body = resp.json()
    assert body["delisting_return_policy"] == -50.0
    c = body["cohort"]
    assert set(c) >= {"scored", "priceable", "measured", "excluded",
                      "presumed_outcomes", "policy"}
    assert c["scored"] == 2 and c["measured"] == 1
    assert c["excluded"]["no_bars"] == 1
    assert c["policy"]["applied_constant"] == 1
    # with-policy overall is present and horizon-keyed as JSON strings.
    assert body["overall_with_policy"] is not None
    assert "5" in body["overall_with_policy"]["mean_excess"]


def test_api_backtest_personas_ci_fields(db_session):
    from fastapi.testclient import TestClient
    from api.main import app

    for i in range(4):
        st = make_stock(db_session, f"PPC{i}")
        _history_row(db_session, st, info_date=MON, composite=80.0)
        repo.upsert_daily_prices(db_session, st.id, _bars(MON, RISING))
        _verdict(db_session, st, "warren_buffett", 9, "BUY")
    _seed_benchmark(db_session, MON)
    db_session.commit()

    with TestClient(app) as client:
        resp = client.get("/api/backtest/personas", params={"benchmark": BENCH})
    assert resp.status_code == 200
    body = resp.json()
    buffett = next(p for p in body["personas"] if p["persona"] == "warren_buffett")
    assert "spread_ci" in buffett
    # Curve points carry the p5/p95 band alongside the mean path.
    assert buffett["curve"]
    assert set(buffett["curve"][0]) >= {"t", "portfolio", "p5", "p95", "n"}
    # Four BUYs and no AVOID: the spread is undefined, so its CI is honestly None.
    assert buffett["spread_ci"]["5"] is None
    assert "spread_ci" in body["subset"]
