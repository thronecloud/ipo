"""P4: analysis retry/dead-letter.

A (stock, persona) pair that keeps failing on the SAME data must stop being
re-planned every day (it silently burned the daily analysis cap). Failures are
persisted per (stock, persona, data_hash); at DEAD_LETTER_THRESHOLD the pair is
skipped by find_work until the data changes (new hash), force=True overrides,
and a later success clears the marker.
"""

import pytest
from sqlalchemy import func, select

from db.models import AnalysisFailure
from engine.analysis.engine import DEAD_LETTER_THRESHOLD, find_work
from engine.repo import (
    add_snapshot,
    clear_analysis_failure,
    extract_columns,
    record_analysis_failure,
)
from factories import make_stock, utc, yf_payload


def _stock_with_snap(session, symbol, price=100.0):
    stock = make_stock(session, symbol)
    payload = yf_payload(price=price)
    snap, _ = add_snapshot(session, stock, payload, extract_columns(payload["info"]),
                           data_quality="full", captured_at=utc(-1))
    session.commit()
    return stock, snap


def _fail_n(session, stock, snap, persona, n):
    for _ in range(n):
        record_analysis_failure(session, stock.id, persona, snap.content_hash, "boom")
    session.commit()


def test_record_failure_increments_and_keeps_last_error(db_session):
    stock, snap = _stock_with_snap(db_session, "DL1")
    n1 = record_analysis_failure(db_session, stock.id, "warren_buffett",
                                 snap.content_hash, "first error")
    n2 = record_analysis_failure(db_session, stock.id, "warren_buffett",
                                 snap.content_hash, "second error")
    db_session.commit()
    assert (n1, n2) == (1, 2)
    row = db_session.scalar(select(AnalysisFailure).where(AnalysisFailure.stock_id == stock.id))
    assert row.failures == 2
    assert row.last_error == "second error"


def test_find_work_skips_dead_lettered_pair(db_session):
    stock, snap = _stock_with_snap(db_session, "DL2")
    _fail_n(db_session, stock, snap, "warren_buffett", DEAD_LETTER_THRESHOLD)
    work = find_work(db_session, ["warren_buffett", "charlie_munger"])
    pairs = {(st.symbol, slug) for st, _, slug in work}
    assert ("DL2", "warren_buffett") not in pairs    # dead-lettered
    assert ("DL2", "charlie_munger") in pairs        # other personas unaffected


def test_below_threshold_still_planned(db_session):
    stock, snap = _stock_with_snap(db_session, "DL3")
    _fail_n(db_session, stock, snap, "warren_buffett", DEAD_LETTER_THRESHOLD - 1)
    work = find_work(db_session, ["warren_buffett"])
    assert [(st.symbol, slug) for st, _, slug in work] == [("DL3", "warren_buffett")]


def test_new_data_hash_revives_pair(db_session):
    """The dead letter is per data_hash: fresh fundamentals mean a fresh chance."""
    stock, snap = _stock_with_snap(db_session, "DL4")
    _fail_n(db_session, stock, snap, "warren_buffett", DEAD_LETTER_THRESHOLD)
    # materially different payload -> new content hash becomes "latest full"
    payload = yf_payload(price=100.0, revenue=9_999.0)
    add_snapshot(db_session, stock, payload, extract_columns(payload["info"]),
                 data_quality="full", captured_at=utc(0))
    db_session.commit()
    work = find_work(db_session, ["warren_buffett"])
    assert [(st.symbol, slug) for st, _, slug in work] == [("DL4", "warren_buffett")]


def test_force_overrides_dead_letter(db_session):
    stock, snap = _stock_with_snap(db_session, "DL5")
    _fail_n(db_session, stock, snap, "warren_buffett", DEAD_LETTER_THRESHOLD)
    work = find_work(db_session, ["warren_buffett"], force=True)
    assert [(st.symbol, slug) for st, _, slug in work] == [("DL5", "warren_buffett")]


def test_clear_failure_removes_marker(db_session):
    stock, snap = _stock_with_snap(db_session, "DL6")
    _fail_n(db_session, stock, snap, "warren_buffett", 2)
    clear_analysis_failure(db_session, stock.id, "warren_buffett", snap.content_hash)
    db_session.commit()
    assert db_session.scalar(select(func.count()).select_from(AnalysisFailure)) == 0


def test_run_incremental_records_and_clears_failures(db_session, monkeypatch):
    """Backend errors persist a failure marker; a later success clears it."""
    import engine.analysis.engine as eng
    stock, snap = _stock_with_snap(db_session, "DL7")

    calls = {"n": 0}

    class FlakyBackend:
        name = "flaky"
        def analyze(self, sysp, userp, model):
            calls["n"] += 1
            if calls["n"] == 1:
                return None, {"error": "backend exploded"}
            return {
                "score": 7, "recommendation": "BUY", "investment_thesis": "ok",
                "key_strengths": ["a"], "key_risks": ["b"], "red_flags": [],
                "detailed_analysis": "d",
                "metrics_evaluated": {
                    "moat_strength": "moderate", "management_quality": "good",
                    "financial_health": "good", "valuation": "fair",
                    "growth_potential": "good",
                },
            }, {}

    monkeypatch.setattr(eng, "get_backend", lambda: FlakyBackend())
    monkeypatch.setattr(eng, "default_model", lambda: "fake-model")

    eng.run_incremental(personas=["warren_buffett"], delay=0, verbose=False)
    row = db_session.scalar(select(AnalysisFailure).where(AnalysisFailure.stock_id == stock.id))
    assert row is not None and row.failures == 1 and "exploded" in (row.last_error or "")

    eng.run_incremental(personas=["warren_buffett"], delay=0, verbose=False)
    db_session.expire_all()
    assert db_session.scalar(select(func.count()).select_from(AnalysisFailure)) == 0


# ---- transient vs permanent classification ----

TRANSIENT = [
    "Claude AI usage limit reached|resets 09:00",
    "rate limit exceeded",
    "CLI timeout after 300s",
    "Error: connection reset by peer",
    "overloaded_error",
    "",
]
PERMANENT = [
    "contract violation: score out of range",
    "invalid json schema",
    "CLI exit 2: unknown flag --nope",
]


@pytest.mark.parametrize("msg", TRANSIENT)
def test_transient_errors_are_classified_transient(msg):
    from engine.analysis.errors import is_transient
    assert is_transient(msg) is True


@pytest.mark.parametrize("msg", PERMANENT)
def test_permanent_errors_are_classified_permanent(msg):
    from engine.analysis.errors import is_transient
    assert is_transient(msg) is False


def test_usage_limit_does_not_increment_dead_letter_counter(db_session):
    """Three quota failures must not permanently drop a pair from coverage."""
    from engine.analysis.engine import is_dead_lettered, record_failure

    stock, snap = _stock_with_snap(db_session, "QUOTA")
    for _ in range(DEAD_LETTER_THRESHOLD):
        record_failure(db_session, stock.id, "warren_buffett", snap.content_hash,
                       "Claude AI usage limit reached")
    db_session.commit()

    assert is_dead_lettered(db_session, stock.id, "warren_buffett", snap.content_hash) is False
    # The pair stays plannable — coverage is preserved, not silently lost.
    work = find_work(db_session, ["warren_buffett"])
    assert [(st.symbol, slug) for st, _, slug in work] == [("QUOTA", "warren_buffett")]


def test_permanent_error_still_dead_letters(db_session):
    """A genuine per-pair defect (bad output) must still stop being re-planned."""
    from engine.analysis.engine import is_dead_lettered, record_failure

    stock, snap = _stock_with_snap(db_session, "BADOUT")
    for _ in range(DEAD_LETTER_THRESHOLD):
        record_failure(db_session, stock.id, "warren_buffett", snap.content_hash,
                       "contract violation: score out of range")
    db_session.commit()

    assert is_dead_lettered(db_session, stock.id, "warren_buffett", snap.content_hash) is True
    work = find_work(db_session, ["warren_buffett"])
    assert [(st.symbol, slug) for st, _, slug in work] == []
