"""Recent-IPO prompt context + parallel persona execution.

1. Recently listed names carry a context block telling personas that missing
   quarters/history are recency artifacts to weigh in conviction — not automatic
   red flags to burn tokens lamenting.
2. run_incremental(workers=N) fans one stock's personas across a thread pool:
   backend calls (the slow part) run concurrently, ALL DB writes stay on the
   main thread — same isolation/dead-letter semantics as the serial path.
"""

import threading
import time

from sqlalchemy import func, select

from db.models import Analysis, AnalysisFailure, CompositeScore
from engine.analysis import engine as eng
from engine.analysis.prompt import RECENT_IPO_CONTEXT, build_user_prompt
from engine.repo import add_snapshot, extract_columns
from factories import make_stock, utc, yf_payload
from src.personas import get_persona_slugs

PERSONAS = get_persona_slugs()


def _snap(session, stock, price=100.0):
    payload = yf_payload(price=price)
    snap, _ = add_snapshot(session, stock, payload, extract_columns(payload["info"]),
                           data_quality="full", captured_at=utc())
    session.commit()
    return snap


def _result(score=7):
    return {
        "score": score, "recommendation": "BUY", "investment_thesis": "Solid.",
        "key_strengths": ["a"], "key_risks": ["b"], "red_flags": [],
        "detailed_analysis": "Detailed.",
        "metrics_evaluated": {
            "moat_strength": "moderate", "management_quality": "good",
            "financial_health": "good", "valuation": "fair",
            "growth_potential": "good",
        },
    }


# ---------- 1. recent-IPO prompt context ----------

def test_recent_ipo_universe_gets_context_block(db_session):
    stock = make_stock(db_session, "RIPO1", universe=["ipo_2026"])
    snap = _snap(db_session, stock)
    prompt = build_user_prompt(db_session, stock, snap)
    assert RECENT_IPO_CONTEXT.strip() in prompt


def test_recent_listing_date_gets_context_block(db_session):
    stock = make_stock(db_session, "RIPO2", listing_date="2026-03-15")
    snap = _snap(db_session, stock)
    assert RECENT_IPO_CONTEXT.strip() in build_user_prompt(db_session, stock, snap)


def test_seasoned_stock_has_no_recency_block(db_session):
    stock = make_stock(db_session, "OLD1", universe=["nse_smallcap"],
                       listing_date="2010-05-01")
    snap = _snap(db_session, stock)
    assert RECENT_IPO_CONTEXT.strip() not in build_user_prompt(db_session, stock, snap)


def test_prompt_version_bumped():
    assert eng.PROMPT_VERSION == "v4"


# ---------- 2. parallel persona execution ----------

class ConcurrencyProbe:
    """Fake backend: records peak concurrent analyze() calls."""

    name = "probe"

    def __init__(self, sleep=0.15, fail_slugs=()):
        self.sleep = sleep
        self.fail_markers = tuple(fail_slugs)
        self.active = 0
        self.max_active = 0
        self.calls = 0
        self._lock = threading.Lock()

    def analyze(self, system_prompt, user_prompt, model):
        with self._lock:
            self.active += 1
            self.calls += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(self.sleep)
        try:
            if any(m in system_prompt for m in self.fail_markers):
                return None, {"error": "boom"}
            return _result(), {"model_used": model}
        finally:
            with self._lock:
                self.active -= 1


def test_workers_run_personas_concurrently_and_save_all(db_session, monkeypatch):
    stock = make_stock(db_session, "PAR1", universe=["ipo_2026"])
    _snap(db_session, stock)
    probe = ConcurrencyProbe()
    monkeypatch.setattr(eng, "get_backend", lambda: probe)

    stats = eng.run_incremental(universe="ipo_2026", model="m", workers=10,
                                delay=0, verbose=False)

    assert probe.calls == len(PERSONAS)
    assert probe.max_active >= 3, f"no real concurrency (max {probe.max_active})"
    assert stats["success"] == len(PERSONAS) and stats["error"] == 0
    assert db_session.scalar(
        select(func.count()).select_from(Analysis).where(Analysis.stock_id == stock.id)
    ) == len(PERSONAS)
    # Composite exists (rescored once after the stock's batch).
    assert db_session.scalar(
        select(CompositeScore).where(CompositeScore.stock_id == stock.id)
    ) is not None


def test_worker_failure_isolated_and_dead_lettered(db_session, monkeypatch):
    stock = make_stock(db_session, "PAR2", universe=["ipo_2026"])
    _snap(db_session, stock)
    # warren_buffett's system prompt uniquely contains "Buffett".
    probe = ConcurrencyProbe(fail_slugs=("Buffett",))
    monkeypatch.setattr(eng, "get_backend", lambda: probe)

    stats = eng.run_incremental(universe="ipo_2026", model="m", workers=10,
                                delay=0, verbose=False)

    assert stats["error"] == 1
    assert stats["success"] == len(PERSONAS) - 1
    failures = db_session.scalars(
        select(AnalysisFailure).where(AnalysisFailure.stock_id == stock.id)
    ).all()
    assert len(failures) == 1 and failures[0].persona == "warren_buffett"


def test_workers_one_matches_serial_semantics(db_session, monkeypatch):
    stock = make_stock(db_session, "PAR3", universe=["ipo_2026"])
    _snap(db_session, stock)
    probe = ConcurrencyProbe(sleep=0)
    monkeypatch.setattr(eng, "get_backend", lambda: probe)

    stats = eng.run_incremental(universe="ipo_2026", model="m", workers=1,
                                delay=0, verbose=False)
    assert stats["success"] == len(PERSONAS)
    assert probe.max_active == 1


def test_backend_string_error_meta_recorded_verbatim(db_session, monkeypatch):
    """CLI error paths return (None, '<string>') — the failure ledger must keep
    that message (it's the ops signal: usage limit vs timeout vs parse error),
    not crash on meta.get and double-count the error."""

    class StringErrorBackend:
        name = "strerr"

        def analyze(self, system_prompt, user_prompt, model):
            return None, "Claude AI usage limit reached|resets 09:00"

    stock = make_stock(db_session, "PAR4", universe=["ipo_2026"])
    _snap(db_session, stock)
    monkeypatch.setattr(eng, "get_backend", lambda: StringErrorBackend())

    stats = eng.run_incremental(universe="ipo_2026", model="m", workers=2,
                                delay=0, verbose=False)

    assert stats["error"] == len(PERSONAS)          # counted ONCE per pair
    failures = db_session.scalars(select(AnalysisFailure)).all()
    assert len(failures) == len(PERSONAS)
    assert "usage limit" in failures[0].last_error
    # A plan-level rate limit is a global outage, not a per-pair defect: the
    # message is recorded for observability, but NONE of the ten may advance the
    # dead-letter counter — otherwise a quota hour silently drops the whole batch.
    assert all(f.failures == 0 for f in failures)


def test_find_work_accepts_screener_backed_fresh_listing(db_session):
    """Fresh listings often never get a 'full' yfinance snapshot, but screener
    carries their prospectus fundamentals — the analysis gate must accept a
    minimal yf snapshot when screener P&L exists, else they stay dark forever."""
    from engine.analysis.engine import find_work

    covered = make_stock(db_session, "FWSCR1", universe=["ipo_2026"])
    payload = yf_payload()
    snap, _ = add_snapshot(db_session, covered, payload,
                           extract_columns(payload["info"]),
                           data_quality="minimal", captured_at=utc())
    add_snapshot(db_session, covered,
                 {"screener": {"ratios": {"Market Cap": "100"},
                               "profit_loss": {"2026": {"Sales": 10}}}},
                 {}, source="screener", data_quality="full", captured_at=utc())

    dark = make_stock(db_session, "FWSCR2", universe=["ipo_2026"])
    dp = yf_payload(price=50.0)
    add_snapshot(db_session, dark, dp, extract_columns(dp["info"]),
                 data_quality="minimal", captured_at=utc())
    db_session.commit()

    work = find_work(db_session, ["warren_buffett"], universe="ipo_2026")
    symbols = {s.symbol for s, _, _ in work}
    assert "FWSCR1" in symbols          # minimal yf + screener P&L -> analyzable
    assert "FWSCR2" not in symbols      # minimal yf, no screener -> still gated
