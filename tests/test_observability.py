"""P4: no silent exception swallows; JobRun honesty on high item-error ratios.

- notify_safe() is the ONLY sanctioned "best-effort notify": it never raises,
  and a failure inside it is logged at WARNING — never `except: pass`.
- A JobRun whose stats show most items failed must not report a clean
  "success": it becomes "partial" so the dashboard/alerting can see it.
"""

import logging
import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from db.models import JobRun
from engine.notify import notify, notify_safe
from engine.repo import job_run


@pytest.fixture()
def notify_transport(monkeypatch):
    """Spy on the network boundary and start from a clean rate-limit slate."""
    import engine.notify as notify_mod

    calls = []
    monkeypatch.setattr(notify_mod, "_post",
                        lambda url, data, headers: calls.append(url))
    monkeypatch.setattr(notify_mod, "_last_sent", {})
    monkeypatch.delenv("NOTIFY_WEBHOOK_URL", raising=False)
    return calls


def test_notify_does_not_send_under_pytest(notify_transport, monkeypatch, capsys):
    """The suite must never page the owner, even with a topic configured.

    tests/test_observability.py drives job_run() with deliberately failing
    batches; db/base.py's load_dotenv() puts the real NTFY_TOPIC in os.environ.
    Without a guard that combination sends real pushes to a real phone.
    """
    monkeypatch.setenv("NTFY_TOPIC", "should-never-be-reached")
    assert "PYTEST_CURRENT_TEST" in os.environ

    assert notify("suppressed-title", "msg") is False
    assert notify_transport == [], "notifier reached the network under pytest"
    assert "suppressed" in capsys.readouterr().out.lower()


def test_notify_sends_when_not_under_pytest(notify_transport, monkeypatch):
    """Production must still page: the guard keys on pytest, not on notifying."""
    monkeypatch.setenv("NTFY_TOPIC", "prod-topic")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    assert notify("live-title", "msg") is True
    assert notify_transport == ["https://ntfy.sh/prod-topic"]


def test_notify_safe_never_raises_and_logs_warning(monkeypatch, caplog):
    import engine.notify as notify_mod

    def boom(*a, **kw):
        raise RuntimeError("notifier exploded")

    monkeypatch.setattr(notify_mod, "notify", boom)
    with caplog.at_level(logging.WARNING):
        assert notify_safe("t", "m") is False   # swallowed, but...
    assert any("notify" in r.message.lower() and r.levelno >= logging.WARNING
               for r in caplog.records)         # ...never silently


def test_notify_safe_passes_through(monkeypatch):
    import engine.notify as notify_mod
    seen = {}
    monkeypatch.setattr(notify_mod, "notify",
                        lambda *a, **kw: seen.update(args=(a, kw)) or True)
    assert notify_safe("title", "msg", priority="high") is True
    assert seen["args"][0] == ("title", "msg")


def test_job_run_high_error_ratio_is_partial(db_session):
    with job_run("honesty_test", target="x") as (session, stats):
        stats.update({"planned": 10, "success": 2, "error": 8})
    j = db_session.scalar(select(JobRun).order_by(JobRun.id.desc()).limit(1))
    assert j.status == "partial"
    assert "error" in (j.error or "").lower() or "8" in (j.error or "")


def test_job_run_low_error_ratio_stays_success(db_session):
    with job_run("honesty_test2", target="x") as (session, stats):
        stats.update({"planned": 10, "success": 9, "error": 1})
    j = db_session.scalar(select(JobRun).order_by(JobRun.id.desc()).limit(1))
    assert j.status == "success"


def test_job_run_no_items_stays_success(db_session):
    with job_run("honesty_test3", target="x") as (session, stats):
        stats["note"] = "no item counters at all"
    j = db_session.scalar(select(JobRun).order_by(JobRun.id.desc()).limit(1))
    assert j.status == "success"


def test_job_run_all_errors_is_partial(db_session):
    """error>0 with zero successes and no explicit total must still be flagged."""
    with job_run("honesty_test4", target="x") as (session, stats):
        stats.update({"success": 0, "error": 5})
    j = db_session.scalar(select(JobRun).order_by(JobRun.id.desc()).limit(1))
    assert j.status == "partial"


def test_scheduler_safe_logs_warning_when_notify_fails(monkeypatch, caplog):
    """A failing tick whose failure-notification ALSO fails must be logged, not eaten."""
    import engine.notify as notify_mod
    from engine.scheduler import safe

    def boom(*a, **kw):
        raise RuntimeError("notifier exploded")

    monkeypatch.setattr(notify_mod, "notify", boom)

    def bad_tick():
        raise ValueError("tick blew up")

    with caplog.at_level(logging.WARNING):
        safe(bad_tick)()   # must not raise
    assert any(r.levelno >= logging.WARNING and "notify" in r.message.lower()
               for r in caplog.records)


def test_grep_no_silent_pass_in_engine():
    """Structural guard: no `except Exception:\\n    pass` anywhere in engine/."""
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parents[1] / "engine"
    offenders = []
    for p in root.rglob("*.py"):
        src = p.read_text()
        if re.search(r"except\s+\w*Exception\w*[^\n]*:\s*\n\s*pass\b", src):
            offenders.append(str(p))
    assert not offenders, f"silent except/pass in: {offenders}"


def test_scheduler_reaps_orphans_recurrently_not_only_at_boot():
    """The event that strands a job_run IS usually the restart, and the boot-time
    reap spares it for being seconds old. Without a recurring tick the row sits at
    'running' until the next restart, and the jobs page polls on it forever.
    """
    from engine.scheduler import build_scheduler

    jobs = {j.id: j for j in build_scheduler().get_jobs()}
    assert "reap" in jobs, "orphan reap must be scheduled, not only run at startup"

    fields = {f.name: str(f) for f in jobs["reap"].trigger.fields}
    assert fields["hour"] == "*", f"reap must run hourly, got hour={fields['hour']}"

    hourly_minutes = {
        jid: {f.name: str(f) for f in j.trigger.fields}["minute"]
        for jid, j in jobs.items()
        if {f.name: str(f) for f in j.trigger.fields}["hour"] == "*"
    }
    assert len(set(hourly_minutes.values())) == len(hourly_minutes), (
        f"hourly jobs collide on the same minute: {hourly_minutes}"
    )


def test_reap_orphaned_runs_marks_stranded_runs_as_error(db_session):
    from engine.repo import JobRun
    from engine.scheduler import REAP_ORPHAN_HOURS, reap_orphaned_runs

    # Both rows sit OUTSIDE the grace window, so only the status filter can
    # tell them apart — the assertion holds whatever the wall-clock date is.
    old = datetime.now(timezone.utc) - timedelta(hours=REAP_ORPHAN_HOURS + 1)
    stranded = JobRun(job_type="analyze", status="running", started_at=old)
    live = JobRun(job_type="score", status="success", started_at=old)
    db_session.add_all([stranded, live])
    db_session.commit()

    reaped = reap_orphaned_runs(db_session)

    db_session.refresh(stranded)
    db_session.refresh(live)
    assert reaped == 1
    assert stranded.status == "error"
    assert "orphaned" in (stranded.error or "")
    assert stranded.finished_at is not None
    assert live.status == "success"


def test_reap_orphaned_runs_spares_recent_runs_and_is_idempotent(db_session):
    """A job legitimately in flight in another container must survive a reap.

    The scheduler restarting must never error a run the api container just
    launched, so only runs older than the grace window are reaped.
    """
    from engine.repo import JobRun
    from engine.scheduler import REAP_ORPHAN_HOURS, reap_orphaned_runs

    now = datetime.now(timezone.utc)
    fresh = JobRun(job_type="analyze", status="running", started_at=now)
    inside = JobRun(job_type="analyze", status="running",
                    started_at=now - timedelta(hours=REAP_ORPHAN_HOURS - 1))
    outside = JobRun(job_type="analyze", status="running",
                     started_at=now - timedelta(hours=REAP_ORPHAN_HOURS + 1))
    db_session.add_all([fresh, inside, outside])
    db_session.commit()

    assert reap_orphaned_runs(db_session) == 1
    for j in (fresh, inside, outside):
        db_session.refresh(j)
    assert fresh.status == "running"
    assert fresh.finished_at is None
    assert inside.status == "running"
    assert inside.finished_at is None
    assert outside.status == "error"

    # Second boot must be a no-op, not a re-write of the row it already reaped.
    reaped_at = outside.finished_at
    assert reap_orphaned_runs(db_session) == 0
    db_session.refresh(outside)
    assert outside.finished_at == reaped_at


# ---------- Task 12: a batch that accomplished nothing is not a success ----------

import engine.ingest.screener_enrich as screener
import engine.ingest.yf_refresh as yfr
from engine import repo
from factories import make_stock, yf_payload


def test_batch_of_all_no_data_is_recorded_as_degraded_not_success():
    """A screener IP ban returns no_data for every stock. That is a failed
    batch, not a successful one."""
    assert repo._item_error_ratio({"processed": 200, "no_data": 200}) == 1.0


def test_batch_of_all_soft_fail_is_recorded_as_degraded():
    assert repo._item_error_ratio({"processed": 150, "soft_fail": 150}) == 1.0


def test_healthy_batch_still_reports_zero():
    assert repo._item_error_ratio({"processed": 100, "ok": 100}) == 0.0


@pytest.fixture()
def notify_spy(monkeypatch):
    """Capture notify_safe calls by title. The engine paths re-import notify_safe
    from engine.notify at call time, so patching the module attribute intercepts
    them; the network is never touched."""
    import engine.notify as notify_mod

    titles = []
    monkeypatch.setattr(notify_mod, "notify_safe",
                        lambda title, message, **kw: titles.append(title) or True)
    return titles


# --- screener_enrich circuit breaker ---

def test_screener_429_aborts_the_batch_and_records_rate_limited(db_session, monkeypatch, notify_spy):
    """A 429 ban makes every remaining page 429 too. Stop hammering the host:
    abort, record rate_limited, and page once — never grind through all 200."""
    for i in range(3):
        make_stock(db_session, f"BANNED{i}", status="active")

    monkeypatch.setattr(screener, "scrape_company_page",
                        lambda url: (None, "429 Client Error: Too Many Requests for url: " + url))

    stats = screener.enrich(delay=0, verbose=False)

    assert stats.get("rate_limited") is True
    assert stats["processed"] < 3, "the batch must abort, not grind through every stock"
    assert notify_spy.count("screener enrichment rate-limited (batch aborted)") == 1


def test_screener_403_also_trips_the_breaker(db_session, monkeypatch, notify_spy):
    make_stock(db_session, "FORBIDDEN", status="active")
    monkeypatch.setattr(screener, "scrape_company_page",
                        lambda url: (None, "403 Client Error: Forbidden for url: " + url))
    stats = screener.enrich(delay=0, verbose=False)
    assert stats.get("rate_limited") is True


def test_screener_healthy_batch_does_not_trip_the_breaker(db_session, monkeypatch, notify_spy):
    make_stock(db_session, "OKSCREEN", status="active")
    good = {"profit_loss": {"Sales": {"2025": 100.0}}, "ratios": {"ROE": "20"},
            "balance_sheet": {}, "cash_flow": {}}
    monkeypatch.setattr(screener, "scrape_company_page", lambda url: (dict(good), None))
    stats = screener.enrich(delay=0, verbose=False)
    assert not stats.get("rate_limited")
    assert stats["processed"] == 1
    assert stats.get("new_snapshot") == 1
    assert notify_spy == []


def test_screener_transient_error_is_not_a_rate_limit(db_session, monkeypatch, notify_spy):
    """A connection timeout is a per-stock failure, not a host ban — it must NOT
    abort the whole batch."""
    for i in range(2):
        make_stock(db_session, f"FLAKY{i}", status="active")
    monkeypatch.setattr(screener, "scrape_company_page",
                        lambda url: (None, "HTTPSConnectionPool: Read timed out."))
    stats = screener.enrich(delay=0, verbose=False)
    assert not stats.get("rate_limited")
    assert stats["processed"] == 2, "a transient error must not trip the breaker"


# --- yf_refresh outage breaker ---

def _fetch_by_symbol(mapping):
    return lambda yf_symbol: mapping[yf_symbol]


def test_yf_outage_parks_nothing_and_notifies_once(db_session, monkeypatch, notify_spy):
    """A yfinance outage soft-fails every stock. That is the source down, not the
    stocks — park none, bump no failure counters, and page exactly once instead of
    once per stock."""
    from db.models import Stock

    for i in range(3):
        make_stock(db_session, f"DOWN{i}", status="active")

    monkeypatch.setattr(yfr, "fetch_payload", lambda sym: (None, "error: yfinance down"))
    stats = yfr.refresh(symbols=["DOWN0", "DOWN1", "DOWN2"], delay=0, verbose=False)

    assert stats["soft_fail"] == 3
    assert stats["parked"] == 0
    assert stats.get("source_outage") is True
    for i in range(3):
        s = db_session.scalar(select(Stock).where(Stock.symbol == f"DOWN{i}"))
        assert s.status == "active", "an outage must not park the universe"
        assert (s.fetch_failures or 0) == 0, "an outage must not penalise stocks"
    assert notify_spy.count("yfinance outage — parking suppressed") == 1


def test_yf_healthy_batch_with_a_few_failures_still_parks(db_session, monkeypatch, notify_spy):
    """A handful of genuine failures in an otherwise-healthy batch (<50%) must still
    park a stock that has crossed the failure threshold."""
    from db.models import Stock

    make_stock(db_session, "GOODA", status="active")
    make_stock(db_session, "GOODB", status="active")
    # Already at PARK_THRESHOLD-1 failures: this batch's soft-fail tips it over.
    make_stock(db_session, "DYING", status="active",
               fetch_failures=yfr.PARK_THRESHOLD - 1)

    mapping = {
        "GOODA.NS": (yf_payload(), "full"),
        "GOODB.NS": (yf_payload(), "full"),
        "DYING.NS": (None, "error: gone"),
    }
    monkeypatch.setattr(yfr, "fetch_payload", _fetch_by_symbol(mapping))
    stats = yfr.refresh(symbols=["GOODA", "GOODB", "DYING"], delay=0, verbose=False)

    assert stats["soft_fail"] == 1
    assert not stats.get("source_outage"), "1/3 failures is not an outage"
    assert stats["parked"] == 1
    dying = db_session.scalar(select(Stock).where(Stock.symbol == "DYING"))
    assert dying.status == "stale"
    assert notify_spy.count("stock auto-parked (repeated fetch failures)") == 1
