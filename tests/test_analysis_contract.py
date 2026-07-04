"""P0-2: LLM output must be validated at the persistence boundary — never persist off-contract."""

import pytest
from sqlalchemy import func, select

from db.models import Analysis
from engine.analysis.contract import AnalysisContractError, validate_analysis_result
from engine.repo import add_snapshot, extract_columns, save_analysis
from factories import make_stock, utc, yf_payload


def _valid_result(score=6, rec="HOLD"):
    return {
        "score": score,
        "recommendation": rec,
        "investment_thesis": "Solid.",
        "key_strengths": ["a"],
        "key_risks": ["b"],
        "red_flags": [],
        "detailed_analysis": "Detailed.",
        "metrics_evaluated": {
            "moat_strength": "moderate",
            "management_quality": "good",
            "financial_health": "good",
            "valuation": "fair",
            "growth_potential": "good",
        },
    }


@pytest.mark.parametrize("mutate,label", [
    (lambda r: "not a dict", "non-dict"),
    (lambda r: {**r, "score": 15}, "score>10"),
    (lambda r: {**r, "score": -3}, "score<0"),
    (lambda r: {**r, "score": 7.5}, "float score"),
    (lambda r: {**r, "score": True}, "bool score"),
    (lambda r: {k: v for k, v in r.items() if k != "score"}, "missing score"),
    (lambda r: {**r, "recommendation": "MAYBE"}, "bad recommendation"),
    (lambda r: {k: v for k, v in r.items() if k != "investment_thesis"}, "missing required key"),
    (lambda r: {**r, "metrics_evaluated": {**r["metrics_evaluated"], "moat_strength": "godlike"}}, "bad enum"),
])
def test_validate_rejects_off_contract(mutate, label):
    with pytest.raises(AnalysisContractError):
        validate_analysis_result(mutate(_valid_result()))


def test_validate_accepts_valid():
    r = _valid_result()
    assert validate_analysis_result(r) is r


def test_save_analysis_rejects_and_writes_nothing(db_session):
    stock = make_stock(db_session, "CONTRACT1")
    payload = yf_payload()
    snap, _ = add_snapshot(db_session, stock, payload, extract_columns(payload["info"]),
                           data_quality="full", captured_at=utc(-1))
    db_session.commit()
    with pytest.raises(AnalysisContractError):
        save_analysis(db_session, stock, snap, "warren_buffett", "m", "v3",
                      {**_valid_result(), "score": 99}, {})
    db_session.rollback()
    assert db_session.scalar(
        select(func.count()).select_from(Analysis).where(Analysis.stock_id == stock.id)
    ) == 0


def test_save_analysis_persists_valid(db_session):
    stock = make_stock(db_session, "CONTRACT2")
    payload = yf_payload()
    snap, _ = add_snapshot(db_session, stock, payload, extract_columns(payload["info"]),
                           data_quality="full", captured_at=utc(-1))
    db_session.commit()
    row = save_analysis(db_session, stock, snap, "warren_buffett", "m", "v3", _valid_result(8, "BUY"), {})
    db_session.commit()
    assert row.score == 8 and row.recommendation == "BUY"


def test_run_incremental_skips_bad_result_and_continues(db_session, monkeypatch):
    """A malformed LLM result increments error and does NOT kill the batch."""
    import engine.analysis.engine as eng
    # two analyzable stocks
    for sym in ("BATCH1", "BATCH2"):
        st = make_stock(db_session, sym, status="active")
        p = yf_payload()
        add_snapshot(db_session, st, p, extract_columns(p["info"]), data_quality="full", captured_at=utc(-1))
    db_session.commit()

    calls = {"n": 0}

    class FakeBackend:
        name = "fake"
        def analyze(self, sysp, userp, model):
            calls["n"] += 1
            if calls["n"] == 1:
                return {**_valid_result(), "score": 42}, {}   # off-contract → must be rejected
            return _valid_result(7, "BUY"), {}                # valid → persisted

    monkeypatch.setattr(eng, "get_backend", lambda: FakeBackend())
    monkeypatch.setattr(eng, "default_model", lambda: "fake-model")
    stats = eng.run_incremental(personas=["warren_buffett"], delay=0, verbose=False)
    assert stats["error"] >= 1 and stats["success"] >= 1
    # exactly one valid analysis persisted (the bad one rejected)
    assert db_session.scalar(select(func.count()).select_from(Analysis)) == 1
