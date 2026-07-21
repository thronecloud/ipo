"""Scheduler reliability — misfire catch-up, price rotation, throttle backoff.

All network is monkeypatched: no yfinance call, no real sleep. Every test is a
unit test of one decision (which jobs are due, what order the rotation visits,
how long to back off, when the circuit trips).
"""

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

import engine.ingest.yf_refresh as yfr
import engine.scheduler as sched
from db.models import JobRun
from factories import make_stock, yf_payload


# ---------- backoff decisions (pure) ----------

class _FixedRNG:
    """Deterministic rng: uniform(a, b) always returns b (max jitter)."""

    @staticmethod
    def uniform(a, b):
        return b


def test_backoff_is_base_delay_while_healthy():
    # No failures → steady, polite base pacing (no backoff creep).
    assert yfr.next_delay(2.0, 0, cap=60.0, jitter=0.0, rng=_FixedRNG) == 2.0


def test_backoff_grows_exponentially_on_consecutive_failures():
    base, cap = 2.0, 60.0
    d1 = yfr.next_delay(base, 1, cap=cap, jitter=0.0, rng=_FixedRNG)
    d2 = yfr.next_delay(base, 2, cap=cap, jitter=0.0, rng=_FixedRNG)
    d3 = yfr.next_delay(base, 3, cap=cap, jitter=0.0, rng=_FixedRNG)
    assert d1 == 4.0 and d2 == 8.0 and d3 == 16.0  # base * 2**n


def test_backoff_is_capped():
    # 2 * 2**10 = 2048 s, clamped to the cap.
    assert yfr.next_delay(2.0, 10, cap=60.0, jitter=0.0, rng=_FixedRNG) == 60.0


def test_backoff_adds_jitter_on_top_of_the_delay():
    # jitter=0.5 → up to +50% of the (capped) delay, so two throttled clients
    # don't re-fire in lockstep.
    assert yfr.next_delay(2.0, 1, cap=60.0, jitter=0.5, rng=_FixedRNG) == 4.0 + 2.0


def test_pacer_resets_backoff_after_a_success():
    pacer = yfr._Pacer(base=2.0, cap=60.0, jitter=0.0, rng=_FixedRNG)
    pacer.record(ok=False)
    pacer.record(ok=False)
    assert pacer.fails == 2
    pacer.record(ok=True)
    assert pacer.fails == 0


# ---------- circuit breaker × outage-breaker interaction ----------

def test_throttle_burst_trips_circuit_and_suppresses_parking(db_session, monkeypatch):
    """A run where yfinance throttles most requests must stop hammering (circuit
    breaker) AND leave the stocks untouched (outage breaker) — no parking, no
    bumped failure counters, no garbage recorded."""
    for i in range(12):
        make_stock(db_session, f"THR{i:02d}", status="active")
    monkeypatch.setattr(yfr, "fetch_payload", lambda sym: (None, "error: rate limited"))

    stats = yfr.refresh(statuses=("active",), delay=0, verbose=False)

    assert stats.get("circuit_broken") is True
    assert stats["processed"] < 12                 # stopped early, didn't drain the batch
    assert stats["processed"] >= yfr.CIRCUIT_MIN_ATTEMPTS
    assert stats.get("source_outage") is True      # classed as an outage
    assert stats["parked"] == 0                     # nobody parked
    # No failure counter bumped — the stocks did nothing wrong.
    rows = db_session.scalars(select(yfr.Stock)).all()
    assert all(s.status == "active" and s.fetch_failures == 0 for s in rows)


def test_healthy_batch_does_not_trip_the_circuit(db_session, monkeypatch):
    for i in range(12):
        make_stock(db_session, f"OK{i:02d}", status="active")
    monkeypatch.setattr(yfr, "fetch_payload", lambda sym: (yf_payload(), "full"))

    stats = yfr.refresh(statuses=("active",), delay=0, verbose=False)

    assert stats.get("circuit_broken") is not True
    assert stats["processed"] == 12                 # whole batch drained


# ---------- price rotation ordering ----------

def test_price_rotation_visits_least_recently_priced_first(db_session, monkeypatch):
    """The daily price pass self-heals: never-priced stocks come first, then the
    stalest bars, so no active name lags the rotation."""
    from engine.repo import upsert_daily_prices

    fresh = make_stock(db_session, "FRESH", status="active")
    stale = make_stock(db_session, "STALE", status="active")
    make_stock(db_session, "NEVER", status="active")     # no bars at all
    upsert_daily_prices(db_session, fresh.id,
                        [{"date": date(2026, 7, 20), "open": 1, "high": 1,
                          "low": 1, "close": 1, "volume": 1}])
    upsert_daily_prices(db_session, stale.id,
                        [{"date": date(2026, 1, 1), "open": 1, "high": 1,
                          "low": 1, "close": 1, "volume": 1}])
    db_session.commit()

    visited: list[str] = []

    def _record(yf_symbol):
        visited.append(yf_symbol)
        return [], False

    monkeypatch.setattr(yfr, "_history_rows", _record)
    yfr.backfill_prices(statuses=("active",), oldest_first=True, delay=0, verbose=False)

    assert visited == ["NEVER.NS", "STALE.NS", "FRESH.NS"]


# ---------- startup misfire catch-up ----------

def _job_run(session, job_type, started_at, status="success"):
    session.add(JobRun(job_type=job_type, target="all", status=status,
                       started_at=started_at, finished_at=started_at))
    session.commit()


def test_catchup_plan_flags_only_stale_daily_jobs(db_session):
    now = datetime.now(timezone.utc)
    _job_run(db_session, "refresh", now - timedelta(days=2))        # stale → due
    _job_run(db_session, "discover_ipos", now - timedelta(hours=1))  # fresh → skip
    # "backfill_prices" has never run at all → due.
    specs = [
        sched.CatchupSpec("refresh", "refresh", timedelta(days=1), lambda: None),
        sched.CatchupSpec("discover", "discover_ipos", timedelta(days=1), lambda: None),
        sched.CatchupSpec("prices", "backfill_prices", timedelta(days=1), lambda: None),
    ]

    due = sched._catchup_plan(db_session, now, specs)

    assert [s.label for s in due] == ["refresh", "prices"]


def test_catchup_ignores_failed_runs_as_last_success(db_session):
    now = datetime.now(timezone.utc)
    _job_run(db_session, "refresh", now - timedelta(hours=1), status="error")
    specs = [sched.CatchupSpec("refresh", "refresh", timedelta(days=1), lambda: None)]

    # The only recent run errored — the job has no fresh SUCCESS, so it is due.
    assert [s.label for s in sched._catchup_plan(db_session, now, specs)] == ["refresh"]


def test_run_catch_up_sequences_due_jobs_and_records_a_job_run(db_session):
    now = datetime.now(timezone.utc)
    order: list[str] = []
    specs = [
        sched.CatchupSpec("a", "job_a", timedelta(days=1), lambda: order.append("a")),
        sched.CatchupSpec("b", "job_b", timedelta(days=1), lambda: order.append("b")),
    ]

    ran = sched.run_catch_up(specs=specs, now=now)

    assert ran == ["a", "b"] and order == ["a", "b"]     # sequenced, in order
    row = db_session.scalar(
        select(JobRun).where(JobRun.job_type == "scheduler_catchup")
        .order_by(JobRun.id.desc())
    )
    assert row is not None and row.stats["caught_up"] == ["a", "b"]


def test_run_catch_up_is_a_noop_when_nothing_is_stale(db_session):
    now = datetime.now(timezone.utc)
    _job_run(db_session, "job_a", now - timedelta(minutes=5))
    specs = [sched.CatchupSpec("a", "job_a", timedelta(days=1), lambda: None)]

    assert sched.run_catch_up(specs=specs, now=now) == []
    assert db_session.scalar(
        select(JobRun).where(JobRun.job_type == "scheduler_catchup")
    ) is None


# ---------- catch-up specs derived from the manifest ----------

def test_catchup_specs_come_from_the_manifest_cadence_aware():
    """The catch-up set is derived from the live schedule, not a hand-kept list: every
    CRON job with a job_type, each carrying its own cadence. Interval and untracked
    jobs are excluded."""
    specs = {s.label: s for s in sched._catchup_specs()}

    # Weekly jobs are covered now (the old list only had 5 daily jobs) with a 7-day
    # cadence, so they aren't re-fired until a whole week + slack is missed.
    assert specs["enrich"].cadence == timedelta(days=7)
    assert specs["refresh_stuck"].cadence == timedelta(days=7)
    # A daily job keeps a one-day cadence.
    assert specs["refresh"].cadence == timedelta(days=1)
    # Interval jobs (price rotation, heartbeat) and untracked jobs (reap) are excluded.
    assert "price_refresh" not in specs
    assert "heartbeat" not in specs
    assert "reap" not in specs


def test_catchup_specs_match_the_manifest_ids():
    from apscheduler.triggers.interval import IntervalTrigger

    manifest = {e.id: e for e in sched.schedule_manifest()}
    expected = {
        e.id for e in manifest.values()
        if e.job_type is not None and not isinstance(e.trigger, IntervalTrigger)
    }
    assert {s.label for s in sched._catchup_specs()} == expected


def _tagged_run(session, job_type, sched_id, started_at, status="success"):
    session.add(JobRun(job_type=job_type, target="all", status=status,
                       started_at=started_at, finished_at=started_at,
                       stats={"sched_id": sched_id}))
    session.commit()


def test_catchup_separates_siblings_that_share_a_job_type(db_session):
    """refresh_stuck writes job_type 'refresh', same as the daily refresh. A recent
    daily refresh must not make the weekly refresh_stuck look fresh — sched_id keeps
    their last-success times apart, so a missed weekly window still catches up."""
    now = datetime.now(timezone.utc)
    _tagged_run(db_session, "refresh", "refresh", now - timedelta(hours=2))       # daily, fresh
    _tagged_run(db_session, "refresh", "refresh_stuck", now - timedelta(days=10)) # weekly, stale

    specs = [
        sched.CatchupSpec("refresh", "refresh", timedelta(days=1), lambda: None),
        sched.CatchupSpec("refresh_stuck", "refresh", timedelta(days=7), lambda: None),
    ]
    due = sched._catchup_plan(db_session, now, specs)
    assert [s.label for s in due] == ["refresh_stuck"]  # only the stale sibling


# ---------- analyze mechanical freeze + skip runs ----------

def test_analysis_enabled_zero_hard_gates_even_with_a_token(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("ANALYSIS_ENABLED", "0")
    assert sched.analysis_available() is False
    monkeypatch.setenv("ANALYSIS_ENABLED", "1")
    assert sched.analysis_available() is True  # token now honoured


def test_frozen_analyze_writes_a_skipped_run_and_does_not_analyze(db_session, monkeypatch):
    monkeypatch.setenv("ANALYSIS_ENABLED", "0")
    called = {"n": 0}
    monkeypatch.setattr(sched, "run_incremental",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    sched.job_analyze_and_score()

    row = db_session.scalar(select(JobRun).where(JobRun.job_type == "analyze")
                            .order_by(JobRun.id.desc()))
    assert row is not None and row.stats["skipped"] == "disabled"
    assert called["n"] == 0  # a frozen tick never spends the plan


def test_uncredentialed_analyze_writes_a_skipped_run(db_session, monkeypatch):
    monkeypatch.setenv("ANALYSIS_ENABLED", "1")
    monkeypatch.setattr(sched, "analysis_available", lambda: False)
    monkeypatch.setattr(sched, "run_incremental", lambda *a, **k: 1 / 0)  # must not run

    sched.job_analyze_and_score()

    row = db_session.scalar(select(JobRun).where(JobRun.job_type == "analyze")
                            .order_by(JobRun.id.desc()))
    assert row.stats["skipped"] == "no_credential"


def test_capped_analyze_writes_a_skipped_run(db_session, monkeypatch):
    monkeypatch.setenv("ANALYSIS_ENABLED", "1")
    monkeypatch.setattr(sched, "analysis_available", lambda: True)
    monkeypatch.setattr(sched, "ANALYZE_DAILY_CAP", 5)
    monkeypatch.setattr(sched, "run_incremental", lambda *a, **k: 1 / 0)  # must not run
    # Today's analyze already spent the cap (success + error both count).
    db_session.add(JobRun(job_type="analyze", target="all", status="success",
                          started_at=datetime.now(timezone.utc),
                          finished_at=datetime.now(timezone.utc),
                          stats={"success": 3, "error": 2}))
    db_session.commit()

    sched.job_analyze_and_score()

    row = db_session.scalar(select(JobRun).where(JobRun.job_type == "analyze",
                                                 JobRun.target == "skipped")
                            .order_by(JobRun.id.desc()))
    assert row is not None and row.stats["skipped"].startswith("daily_cap")


def test_skipped_runs_do_not_advance_the_daily_cap(db_session, monkeypatch):
    # A skipped tick carries no success/error, so it must count as zero against the cap
    # — otherwise a run of skips would look like spend and suppress real analysis.
    monkeypatch.setenv("ANALYSIS_ENABLED", "0")
    sched.job_analyze_and_score()
    sched.job_analyze_and_score()
    assert sched.analyses_done_today() == 0
