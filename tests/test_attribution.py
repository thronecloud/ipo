"""Factor attribution: skill vs tilt.

Separates a genuine stock-picking edge from a size or sector bet that beats a
single smallcap benchmark without picking anything. The discriminating test is
``test_residual_ic_zero_when_raw_ic_is_pure_sector_tilt`` — a cohort whose whole
raw IC is a between-sector spread must collapse to ~0 once the sector tilt is
regressed out.
"""

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from api.main import app
from engine import repo
from engine.backtest.attribution import (
    MIN_ATTRIBUTION_STOCKS,
    _ols,
    _solve,
    run_attribution,
)
from tests.factories import make_stock

H = 21  # the attribution horizon used throughout the dict-level tests


# ---------- row helpers ----------

def _row(symbol: str, composite: float, sector: str | None, mcap: float | None,
         ret: float, excess: float | None = None) -> dict:
    """One scored, priced cohort member in the shape run_attribution consumes."""
    ex = ret if excess is None else excess
    return {
        "symbol": symbol,
        "composite": composite,
        "sector": sector,
        "mcap": mcap,
        "returns": {H: ret},
        "excess": {H: ex},
    }


# ---------- OLS correctness (hand-computable) ----------

def test_solve_recovers_known_solution():
    # 2x + y = 5 ; x + 3y = 10  ->  x = 1, y = 3.
    sol = _solve([[2.0, 1.0], [1.0, 3.0]], [5.0, 10.0])
    assert sol is not None
    assert abs(sol[0] - 1.0) < 1e-12 and abs(sol[1] - 3.0) < 1e-12


def test_solve_returns_none_when_singular():
    # Second row is 2x the first: rank-deficient, no unique solution.
    assert _solve([[1.0, 2.0], [2.0, 4.0]], [3.0, 6.0]) is None


def test_ols_recovers_intercept_and_slope():
    # y = 2 + 3x exactly, design [1, x]: intercept 2, slope 3, R^2 = 1.
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    x = [[1.0, v] for v in xs]
    y = [2.0 + 3.0 * v for v in xs]
    beta, resid, r2 = _ols(x, y)
    assert abs(beta[0] - 2.0) < 1e-9
    assert abs(beta[1] - 3.0) < 1e-9
    assert all(abs(e) < 1e-9 for e in resid)
    assert abs(r2 - 1.0) < 1e-12


def test_ols_recovers_size_and_sector_loadings():
    # y = 2 + 3*x + 5*dummy exactly. Design [1, x, dummy].
    data = [(1.0, 0), (2.0, 0), (3.0, 1), (4.0, 1), (5.0, 0), (6.0, 1)]
    x = [[1.0, v, float(d)] for v, d in data]
    y = [2.0 + 3.0 * v + 5.0 * d for v, d in data]
    beta, _resid, r2 = _ols(x, y)
    assert abs(beta[0] - 2.0) < 1e-9
    assert abs(beta[1] - 3.0) < 1e-9
    assert abs(beta[2] - 5.0) < 1e-9
    assert abs(r2 - 1.0) < 1e-12


# ---------- sector pooling + rank-deficiency guards ----------

def test_small_sectors_pool_into_other():
    rows = []
    for i in range(5):
        rows.append(_row(f"IT{i}", 60 + i, "IT", 100 + i, 5 + i))
    for i in range(4):
        rows.append(_row(f"PH{i}", 50 + i, "Pharma", 200 + i, 4 + i))
    for i in range(2):  # below MIN_SECTOR_FOR_DUMMY -> pools into "other"
        rows.append(_row(f"TN{i}", 40 + i, "Tiny", 300 + i, 3 + i))

    block = run_attribution(rows, H, n_boot=200)
    assert not block["insufficient"]
    assert "Tiny" in block["pooled_into_other"]
    assert block["modeled_sectors"] == ["IT", "Pharma"]
    assert block["reference_sector"] == "other"  # the pooled group is the baseline


def test_top_sectors_capped_rest_pooled():
    # 10 named sectors of 3 stocks each: only the top 8 keep a dummy, 2 pool.
    rows = []
    n = 0
    for s in range(10):
        for _ in range(3):
            rows.append(_row(f"S{s}_{n}", 50 + (n % 7), f"Sec{s:02d}", 100 + n, (n % 5) - 2))
            n += 1
    block = run_attribution(rows, H, n_boot=100)
    assert not block["insufficient"]
    assert len(block["modeled_sectors"]) == 8
    assert len(block["pooled_into_other"]) == 2


def test_singular_design_drops_dummies():
    # Market cap is constant within each sector, so the size column is an exact
    # linear combination of the intercept and the sector dummy -> singular. The
    # guard must shed the dummies and keep the (still identified) size term.
    rows = []
    for i in range(5):
        rows.append(_row(f"A{i}", 60 + i, "SecA", 100.0, 5 + i))
    for i in range(5):
        rows.append(_row(f"B{i}", 50 + i, "SecB", 200.0, -3 + i))
    block = run_attribution(rows, H, n_boot=100)
    assert block["dummies_dropped"] is True
    assert block["modeled_sectors"] == []
    assert block["sector_loadings"] == {}
    assert block["size_dropped"] is False
    assert block["size_loading"] is not None


# ---------- the discriminating test: tilt vs skill ----------

def test_residual_ic_zero_when_raw_ic_is_pure_sector_tilt():
    # Two sectors. Between them, composite and return move together (high sector
    # loads high, low loads low) -> a strong RAW IC. But WITHIN each sector the
    # score has zero rank relationship with the return. Once the sector tilt is
    # regressed out, the residual IC must collapse to ~0.
    #
    # Equal market cap -> the size term is degenerate and drops out cleanly,
    # leaving intercept + one sector dummy, which recovers the two sector means
    # exactly. The residuals are then the within-sector deviations, constructed
    # so their global rank correlation with the composite is exactly zero.
    a_comp = [70, 72, 74, 76, 78]
    a_ret = [29, 32, 30, 28, 31]  # mean 30; deviations [-1, 2, 0, -2, 1]
    b_comp = [40, 42, 44, 46, 48]
    b_ret = [-11, -8, -10, -12, -9]  # mean -10; same deviation pattern
    rows = []
    for i in range(5):
        rows.append(_row(f"SA{i}", a_comp[i], "SecA", 100.0, a_ret[i]))
        rows.append(_row(f"SB{i}", b_comp[i], "SecB", 100.0, b_ret[i]))

    block = run_attribution(rows, H, n_boot=300)
    assert not block["insufficient"]
    assert block["size_dropped"] is True          # equal mcap -> size degenerate
    assert block["dummies_dropped"] is False       # the sector dummy is identified
    assert block["modeled_sectors"] == ["SecB"]
    assert block["reference_sector"] == "SecA"

    assert block["raw_ic"]["point"] > 0.7          # the tilt-driven headline
    assert abs(block["residual_ic"]["point"]) < 1e-9  # nothing left after the strip


def test_residual_ic_survives_genuine_within_sector_skill():
    # The complement: the score picks winners WITHIN each sector and the sectors
    # have identical mean returns. Stripping the (null) sector tilt must leave the
    # skill intact -> residual IC stays high.
    rows = []
    for i in range(5):
        rows.append(_row(f"WA{i}", 50 + 2 * i, "SecA", 100.0, 1 + i))  # +1..+5
        rows.append(_row(f"WB{i}", 51 + 2 * i, "SecB", 100.0, 1 + i))
    block = run_attribution(rows, H, n_boot=300)
    assert block["raw_ic"]["point"] > 0.8
    assert block["residual_ic"]["point"] > 0.8


# ---------- per-sector IC thresholds (the B3 refusal pattern) ----------

def test_per_sector_ic_threshold():
    rows = []
    for i in range(8):  # >= MIN_SECTOR_STOCKS: composite orders return -> IC ~ 1
        rows.append(_row(f"BIG{i}", 40 + i, "Big", 100 + i, i))
    for i in range(5):  # below threshold -> insufficient
        rows.append(_row(f"SML{i}", 40 + i, "Small", 300 + i, i))

    block = run_attribution(rows, H, n_boot=200)
    big = block["per_sector_ic"]["Big"]
    small = block["per_sector_ic"]["Small"]
    assert "insufficient" not in big
    assert big["point"] > 0.9
    assert small == {"insufficient": True, "n": 5}


def test_refuses_below_minimum_cohort():
    rows = [_row(f"X{i}", 50 + i, "IT", 100 + i, i) for i in range(MIN_ATTRIBUTION_STOCKS - 1)]
    block = run_attribution(rows, H, n_boot=50)
    assert block["insufficient"] is True
    assert block["alpha"] is None
    assert block["raw_ic"] is None
    assert str(MIN_ATTRIBUTION_STOCKS) in block["reason"]


# ---------- API contract: the block flows through /api/backtest ----------

BENCH = "BSE-SMLCAP.BO"
MON = date(2026, 1, 5)


def _bars(start: date, closes: list[float]) -> list[dict]:
    rows, d, i = [], start, 0
    while i < len(closes):
        if d.weekday() < 5:
            c = closes[i]
            rows.append({"date": d, "open": c, "high": round(c * 1.005, 4),
                         "low": round(c * 0.995, 4), "close": c, "volume": 5000})
            i += 1
        d += timedelta(days=1)
    return rows


def _history(session, stock, composite: float):
    from db.models import CompositeScoreHistory
    session.add(CompositeScoreHistory(
        stock_id=stock.id,
        as_of_date=MON,
        information_date=datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc),
        composite_score=composite,
        lcb=composite - 10,
        confidence_tier="high",
        consensus_recommendation="HOLD",
        factor_version="axis-v1",
        analysis_coverage=10,
    ))
    session.commit()


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def test_backtest_endpoint_carries_attribution(client, db_session):
    closes = [100.0, 100.0] + [100.0 + 3 * i for i in range(1, 25)]  # rises to horizon 21
    for i in range(10):
        sector = "IT" if i % 2 == 0 else "Pharma"
        st = make_stock(db_session, f"ATTR{i}", sector=sector, ipo_mcap_cr=100.0 + 10 * i)
        _history(db_session, st, composite=50.0 + i)
        repo.upsert_daily_prices(db_session, st.id, _bars(MON, closes))
    repo.upsert_index_prices(db_session, BENCH, _bars(MON, [10000.0] * 40))
    db_session.commit()

    r = client.get("/api/backtest", params={"benchmark": BENCH})
    assert r.status_code == 200
    attr = r.json()["attribution"]

    # Contract: the block is present with the expected shape and the chosen
    # horizon flows through as an int in the JSON payload.
    assert attr["horizon"] == 21
    assert set(attr) >= {
        "horizon", "n", "measured", "insufficient", "reference_sector",
        "modeled_sectors", "pooled_into_other", "alpha", "size_loading",
        "sector_loadings", "r2", "raw_ic", "residual_ic", "per_sector_ic",
        "dummies_dropped", "size_dropped",
    }
    assert attr["n"] == 10
    assert not attr["insufficient"]
