"""Engine Room scheduler dashboard — the manifest that joins EXPECTED (the declared
schedule) against ACTUAL (job_runs), and the missed-run detection built on it.

Three concerns:
  1. manifest completeness — every scheduler job is described and mapped to a real
     job_runs.job_type (or is honestly marked untracked). Adding an add_job without
     updating _JOB_META must fail here.
  2. missed detection — a job that skipped a whole cadence is flagged; one that ran
     on time (or merely a little late) is not; untracked jobs report unknown.
  3. API contract — GET /api/admin/scheduler joins the two and drills down per job.
"""

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api.routers.admin as admin_router
from api.main import app
from db.models import JobRun
from engine.scheduler import build_scheduler, schedule_manifest

UTC = timezone.utc
PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _entry(job_id: str):
    return next(e for e in schedule_manifest() if e.id == job_id)


def _seed_run(session, job_type, *, minutes_ago, status="success"):
    started = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    row = JobRun(job_type=job_type, target="all", status=status,
                 started_at=started, finished_at=started, stats={"processed": 3})
    session.add(row)
    session.commit()
    return row


def _real_job_types() -> set[str]:
    """Every job_type that source actually passes to job_run() — the ground truth
    the manifest mapping must agree with."""
    found: set[str] = set()
    for root in ("engine", "src"):
        for path in (PROJECT_ROOT / root).rglob("*.py"):
            found.update(re.findall(r'job_run\(\s*"([^"]+)"', path.read_text()))
    return found


# ---------- manifest completeness ----------

def test_every_scheduler_job_is_in_the_manifest():
    """Adding an add_job() without a _JOB_META entry must break the build."""
    scheduled = {j.id for j in build_scheduler().get_jobs()}
    described = {e.id for e in schedule_manifest()}
    assert scheduled == described


def test_manifest_job_types_map_to_real_job_run_calls():
    real = _real_job_types()
    for e in schedule_manifest():
        if e.job_type is None:
            continue  # untracked by design (reap writes no job_run)
        assert e.job_type in real, f"{e.id} maps to unknown job_type {e.job_type!r}"


def test_reap_is_marked_untracked():
    # reap uses a bare UPDATE with no job_run wrapper — it must not pretend to be
    # observable, or the dashboard would show a permanently-missing last run.
    assert _entry("reap").job_type is None


def test_cadence_text_is_human_readable():
    got = {e.id: e.cadence for e in schedule_manifest()}
    assert got["refresh"] == "daily 02:00 UTC"
    assert got["price_refresh"] == "every 2h"
    assert got["enrich"] == "Sun 03:00 UTC"
    assert got["dq_fill"] == "Sat 04:00 UTC"
    assert got["amfi"] == "monthly day 5 04:00 UTC"
    assert got["analyze"] == "hourly :30"


# ---------- missed detection (frozen now) ----------

def test_cron_job_on_time_is_not_missed(db_session):
    now = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    _seed_run(db_session, "refresh", minutes_ago=60)   # ran an hour ago
    job = admin_router._scheduler_job(db_session, _entry("refresh"), now, drill=False)
    assert job.missed is False
    assert job.last_run is not None
    assert job.next_expected > now                      # next fire is in the future


def test_cron_job_that_skipped_a_full_cadence_is_missed(db_session):
    now = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    # daily job last succeeded 3 days ago → whole cycles skipped → MISSED.
    db_session.add(JobRun(job_type="refresh", target="all", status="success",
                          started_at=now - timedelta(days=3),
                          finished_at=now - timedelta(days=3)))
    db_session.commit()
    job = admin_router._scheduler_job(db_session, _entry("refresh"), now, drill=False)
    assert job.missed is True
    assert job.next_expected < now


def test_interval_job_missed_after_two_intervals(db_session):
    now = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    entry = _entry("price_refresh")     # every 2h, grace = one interval
    db_session.add(JobRun(job_type="backfill_prices", target="all", status="success",
                          started_at=now - timedelta(hours=1),
                          finished_at=now - timedelta(hours=1)))
    db_session.commit()
    fresh = admin_router._scheduler_job(db_session, entry, now, drill=False)
    assert fresh.missed is False        # 1h ago, well inside 2h + 2h grace

    db_session.query(JobRun).delete()
    db_session.add(JobRun(job_type="backfill_prices", target="all", status="success",
                          started_at=now - timedelta(hours=5),
                          finished_at=now - timedelta(hours=5)))
    db_session.commit()
    stale = admin_router._scheduler_job(db_session, entry, now, drill=False)
    assert stale.missed is True         # 5h ago > interval(2h) + grace(2h)


def test_untracked_job_reports_unknown_missed(db_session):
    now = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    job = admin_router._scheduler_job(db_session, _entry("reap"), now, drill=False)
    assert job.missed is None
    assert job.last_run is None
    assert job.next_expected is not None   # still shows when it will next fire


def test_tracked_job_with_no_history_is_missed(db_session):
    now = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    job = admin_router._scheduler_job(db_session, _entry("dq_fill"), now, drill=False)
    assert job.missed is True
    assert job.last_run is None


# ---------- API contract ----------

def test_scheduler_endpoint_covers_all_jobs(client, db_session):
    _seed_run(db_session, "refresh", minutes_ago=30)
    r = client.get("/api/admin/scheduler")
    assert r.status_code == 200
    body = r.json()
    assert "now" in body
    ids = {j["id"] for j in body["jobs"]}
    assert ids == {e.id for e in schedule_manifest()}
    for j in body["jobs"]:
        assert j["description"] and j["cadence"]
        assert j["trigger_kind"] in ("cron", "interval")
    refresh = next(j for j in body["jobs"] if j["id"] == "refresh")
    assert refresh["last_run"] is not None
    assert refresh["recent_runs"] == []   # no drill-down param


def test_scheduler_drilldown_returns_recent_runs(client, db_session):
    for i in range(3):
        _seed_run(db_session, "analyze", minutes_ago=30 * (i + 1))
    r = client.get("/api/admin/scheduler?job=analyze")
    assert r.status_code == 200
    body = r.json()
    analyze = next(j for j in body["jobs"] if j["id"] == "analyze")
    assert len(analyze["recent_runs"]) == 3
    # other jobs are not drilled
    other = next(j for j in body["jobs"] if j["id"] == "refresh")
    assert other["recent_runs"] == []
