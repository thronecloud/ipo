"""Walk-forward vintages + prompt-era attribution.

Guards the properties that make a rolling study honest:
- No lookahead ACROSS step boundaries: a verdict dated after a formation date T
  is absent from window T and only enters a later window (seeded proof).
- Era stratification by dominant prompt_version / model, with an explicit
  insufficient-n refusal rather than a fabricated number.
- IC-decay averaging across the same windows.
- The /api/backtest/vintages contract.
"""

from datetime import date, datetime, timedelta, timezone

from db.models import CompositeScoreHistory
from engine import repo
from engine.backtest.vintages import run_vintage_study
from tests.factories import make_stock


BENCH = "BSE-SMLCAP.BO"
MON = date(2026, 1, 5)  # a Monday


def _weekday_dates(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _bars(start: date, closes: list[float]) -> list[dict]:
    # Nonzero intraday range (high != low) so ordinary bars are not read as
    # zero-range circuit locks; open == close for the next-open entry basis.
    dates = _weekday_dates(start, len(closes))
    return [{"date": d, "open": c, "high": round(c * 1.005, 4),
             "low": round(c * 0.995, 4), "close": c, "volume": 1000}
            for d, c in zip(dates, closes)]


def _hist(session, stock, *, info_date: date, composite: float,
          prompt: str = "v1", model: str = "sonnet") -> CompositeScoreHistory:
    row = CompositeScoreHistory(
        stock_id=stock.id,
        as_of_date=info_date,
        information_date=datetime(info_date.year, info_date.month, info_date.day,
                                  12, 0, tzinfo=timezone.utc),
        composite_score=composite,
        lcb=composite - 10,
        confidence_tier="high",
        consensus_recommendation="HOLD",
        analysis_coverage=10,
        prompt_versions={prompt: 10},
        models_used={model: 10},
    )
    session.add(row)
    session.commit()
    return row


def _seed_benchmark(session, start: date, n: int, base: float = 10000.0):
    # Flat benchmark so excess return == raw return — assertions stay exact.
    repo.upsert_index_prices(session, BENCH, _bars(start, [base] * n))
    session.commit()


# ── no lookahead across step boundaries ───────────────────────────

def test_window_formation_excludes_verdict_dated_after_T(db_session):
    # A anchors the earliest view at MON so windows start there. B's view forms
    # only at tdates[10] — between window 0 (tdates[0]) and window 1 (tdates[20]).
    # It must be absent from window 0's cohort and present in window 1's.
    days = _weekday_dates(MON, 160)
    a = make_stock(db_session, "VW_A")
    b = make_stock(db_session, "VW_B")
    _hist(db_session, a, info_date=days[0], composite=70.0)
    _hist(db_session, b, info_date=days[10], composite=80.0)
    repo.upsert_daily_prices(db_session, a.id, _bars(MON, [100.0] * 160))
    repo.upsert_daily_prices(db_session, b.id, _bars(MON, [100.0] * 160))
    _seed_benchmark(db_session, MON, 160)
    db_session.commit()

    report = run_vintage_study(db_session, benchmark=BENCH, step_days=20,
                               hold_days=20, n_boot=200)
    windows = report["windows"]
    assert windows[0]["date"] == days[0]
    assert windows[1]["date"] == days[20]
    # B's verdict (dated days[10]) is a lookahead at window 0, known by window 1.
    assert windows[0]["n_cohort"] == 1   # A only
    assert windows[1]["n_cohort"] == 2   # A + B


# ── era stratification + insufficient-n refusal ───────────────────

def test_era_stratification_and_insufficient_n(db_session):
    # 12 names on the v1/sonnet era (sufficient), 3 on v5/fable (below MIN_ERA_N).
    # The sufficient era reports numbers; the thin one is refused, not faked.
    for i in range(12):
        st = make_stock(db_session, f"ERA_OLD{i}")
        _hist(db_session, st, info_date=MON, composite=70.0 + i,
              prompt="v1", model="sonnet")
        repo.upsert_daily_prices(db_session, st.id,
                                 _bars(MON, [100.0, 100.0, 110.0, 111.0, 112.0, 113.0, 114.0, 115.0]))
    for i in range(3):
        st = make_stock(db_session, f"ERA_NEW{i}")
        _hist(db_session, st, info_date=MON, composite=60.0 + i,
              prompt="v5", model="fable")
        repo.upsert_daily_prices(db_session, st.id,
                                 _bars(MON, [100.0, 100.0, 110.0, 111.0, 112.0, 113.0, 114.0, 115.0]))
    _seed_benchmark(db_session, MON, 40)
    db_session.commit()

    report = run_vintage_study(db_session, benchmark=BENCH, step_days=100,
                               hold_days=5, n_boot=200, min_era_n=10)
    assert len(report["windows"]) == 1
    eras = report["windows"][0]["eras"]

    v1 = eras["prompt_version"]["v1"]
    assert v1["insufficient"] is False
    assert v1["era_n"] == 12 and v1["n"] == 12
    assert v1["mean_excess"] is not None

    v5 = eras["prompt_version"]["v5"]
    assert v5["insufficient"] is True
    assert v5["n"] == 3
    assert "mean_excess" not in v5  # refused, no fabricated number

    # Same split on the model axis.
    assert eras["model"]["sonnet"]["insufficient"] is False
    assert eras["model"]["fable"]["insufficient"] is True


# ── IC decay math ─────────────────────────────────────────────────

def test_ic_decay_averages_per_window_ics(db_session):
    # Composite orders the 1-day return perfectly (IC +1) and inverts the 2-day
    # return perfectly (IC -1). One window, so the decay curve is exactly those.
    for sym, comp, x1, x2 in (("ICD1", 90.0, 130.0, 90.0),
                              ("ICD2", 70.0, 110.0, 100.0),
                              ("ICD3", 50.0, 90.0, 110.0)):
        st = make_stock(db_session, sym)
        _hist(db_session, st, info_date=MON, composite=comp)
        repo.upsert_daily_prices(db_session, st.id, _bars(MON, [100.0, 100.0, x1, x2]))
    _seed_benchmark(db_session, MON, 40)
    db_session.commit()

    report = run_vintage_study(db_session, benchmark=BENCH, step_days=1000,
                               hold_days=1, decay_horizons=(1, 2), n_boot=200)
    assert len(report["windows"]) == 1
    win = report["windows"][0]
    assert abs(win["ic_by_horizon"][1] - 1.0) < 1e-9
    assert abs(win["ic_by_horizon"][2] + 1.0) < 1e-9

    decay = {d["horizon"]: d for d in report["ic_decay"]}
    assert abs(decay[1]["ic"] - 1.0) < 1e-9 and decay[1]["n_windows"] == 1
    assert abs(decay[2]["ic"] + 1.0) < 1e-9 and decay[2]["n_windows"] == 1


def test_window_mean_excess_ci_present(db_session):
    # Six rising names over a flat benchmark: the per-window mean-excess CI must
    # clear zero (verdict positive), same block-bootstrap contract as the study.
    for i in range(6):
        st = make_stock(db_session, f"VCI{i}")
        _hist(db_session, st, info_date=MON, composite=70.0 + i)
        repo.upsert_daily_prices(db_session, st.id, _bars(MON, [100.0, 100.0, 110.0]))
    _seed_benchmark(db_session, MON, 40)
    db_session.commit()

    report = run_vintage_study(db_session, benchmark=BENCH, step_days=1000,
                               hold_days=1, decay_horizons=(1,), n_boot=500)
    win = report["windows"][0]
    ci = win["mean_excess_ci"]
    assert ci is not None and ci["ci_low"] > 0 and ci["verdict"] == "positive"
    assert win["hit_rate_ci"]["verdict"] == "positive"
    # Net-of-friction twin of the window mean excess: present and strictly worse
    # than gross for a positive-return window.
    assert win["net_mean_excess"] is not None
    assert win["net_mean_excess"] < win["mean_excess"]
    assert win["net_mean_excess_ci"] is not None


# ── zero-window explanation ───────────────────────────────────────

def test_zero_windows_emits_explanatory_note(db_session):
    # A 5-bar benchmark leaves no forward room for a 63-day hold, so no window
    # forms. The payload must SAY so, not return a silently empty windows list.
    st = make_stock(db_session, "NOWIN")
    _hist(db_session, st, info_date=MON, composite=70.0)
    repo.upsert_daily_prices(db_session, st.id, _bars(MON, [100.0] * 5))
    _seed_benchmark(db_session, MON, 5)
    db_session.commit()

    report = run_vintage_study(db_session, benchmark=BENCH, step_days=21,
                               hold_days=63, n_boot=50)
    assert report["windows"] == []
    assert "note" in report and "hold=63" in report["note"]


# ── API contract ──────────────────────────────────────────────────

def test_api_vintages_contract(db_session):
    from fastapi.testclient import TestClient
    from api.main import app

    for i in range(3):
        st = make_stock(db_session, f"VAPI{i}")
        _hist(db_session, st, info_date=MON, composite=70.0 + i)
        repo.upsert_daily_prices(db_session, st.id, _bars(MON, [100.0, 100.0, 110.0]))
    _seed_benchmark(db_session, MON, 60)
    db_session.commit()

    with TestClient(app) as client:
        resp = client.get("/api/backtest/vintages",
                          params={"benchmark": BENCH, "step": 100, "hold": 5})
    assert resp.status_code == 200
    body = resp.json()
    assert body["benchmark"] == BENCH
    assert body["step_days"] == 100 and body["hold_days"] == 5
    assert set(body) >= {"benchmark", "step_days", "hold_days", "decay_horizons",
                         "window_dates", "windows", "ic_decay", "min_era_n"}
    assert body["windows"]
    win = body["windows"][0]
    # date serialises as an ISO string; horizon dicts arrive JSON-stringified.
    assert win["date"] == MON.isoformat()
    assert set(win) >= {"date", "n_cohort", "mean_excess", "mean_excess_ci",
                        "hit_rate", "ic", "eras", "ic_by_horizon"}
    assert set(win["eras"]) == {"prompt_version", "model"}
    decay = body["ic_decay"]
    assert decay and set(decay[0]) >= {"horizon", "ic", "ic_ci", "n_windows"}


def test_api_vintages_rejects_out_of_range_step(db_session):
    from fastapi.testclient import TestClient
    from api.main import app

    with TestClient(app) as client:
        resp = client.get("/api/backtest/vintages", params={"step": 0})
    assert resp.status_code == 422  # ge=1 guard
