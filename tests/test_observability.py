"""P4: no silent exception swallows; JobRun honesty on high item-error ratios.

- notify_safe() is the ONLY sanctioned "best-effort notify": it never raises,
  and a failure inside it is logged at WARNING — never `except: pass`.
- A JobRun whose stats show most items failed must not report a clean
  "success": it becomes "partial" so the dashboard/alerting can see it.
"""

import logging

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
