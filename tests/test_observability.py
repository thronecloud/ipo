"""P4: no silent exception swallows; JobRun honesty on high item-error ratios.

- notify_safe() is the ONLY sanctioned "best-effort notify": it never raises,
  and a failure inside it is logged at WARNING — never `except: pass`.
- A JobRun whose stats show most items failed must not report a clean
  "success": it becomes "partial" so the dashboard/alerting can see it.
"""

import logging
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from db.models import JobRun
from engine.notify import notify_safe
from engine.repo import job_run


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
