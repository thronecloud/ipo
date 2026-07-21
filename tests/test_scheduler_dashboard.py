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


def _seed_run(session, job_type, *, minutes_ago, status="success", sched_id=None):
    started = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    stats = {"processed": 3}
    if sched_id is not None:
        stats["sched_id"] = sched_id
    row = JobRun(job_type=job_type, target="all", status=status,
                 started_at=started, finished_at=started, stats=stats)
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


# ---------- grace boundary + state field ----------

def test_weekly_job_is_missed_within_a_day_not_a_full_week(db_session):
    """A weekly job that skipped its Sunday run must flag within 24h — not stay green
    for the rest of the week. The grace is capped at 24h, so a skipped weekly window
    surfaces the day after it was due, not a full cadence later."""
    entry = _entry("enrich")  # Sun 03:00 UTC, cadence 7 days
    last_sun = datetime(2026, 7, 5, 3, 0, tzinfo=UTC)
    db_session.add(JobRun(job_type="screener_enrich", target="all", status="success",
                          started_at=last_sun, finished_at=last_sun,
                          stats={"sched_id": "enrich"}))
    db_session.commit()

    # 33h past the next expected Sunday (07-12 03:00) — inside the week, past 24h grace.
    now = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
    job = admin_router._scheduler_job(db_session, entry, now, drill=False)
    assert job.missed is True and job.state == "missed"


def test_state_is_due_when_past_expected_but_inside_grace(db_session):
    entry = _entry("refresh")  # daily 02:00 UTC
    last = datetime(2026, 7, 19, 2, 0, tzinfo=UTC)
    db_session.add(JobRun(job_type="refresh", target="all", status="success",
                          started_at=last, finished_at=last,
                          stats={"sched_id": "refresh"}))
    db_session.commit()

    # 10h past the next expected fire (07-20 02:00), still inside the 24h grace.
    now = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    job = admin_router._scheduler_job(db_session, entry, now, drill=False)
    assert job.state == "due" and job.missed is False


def test_state_is_ok_when_next_fire_is_still_ahead(db_session):
    now = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    _seed_run(db_session, "refresh", minutes_ago=60, sched_id="refresh")
    job = admin_router._scheduler_job(db_session, _entry("refresh"), now, drill=False)
    assert job.state == "ok" and job.missed is False


def test_state_never_ran_and_untracked(db_session):
    now = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    never = admin_router._scheduler_job(db_session, _entry("dq_fill"), now, drill=False)
    assert never.state == "never_ran" and never.missed is True

    untracked = admin_router._scheduler_job(db_session, _entry("reap"), now, drill=False)
    assert untracked.state == "untracked" and untracked.missed is None


def test_disabled_gate_overrides_the_timing_verdict(db_session):
    now = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    # Even with no run on record (which would be never_ran/missed), a gated analyze
    # reads as intentionally idle, never as a missed run.
    job = admin_router._scheduler_job(db_session, _entry("analyze"), now,
                                      drill=False, disabled=True)
    assert job.state == "disabled" and job.missed is False


# ---------- sched_id disambiguation (shared job_type) ----------

def test_shared_job_type_runs_are_attributed_by_sched_id(db_session):
    """refresh and refresh_stuck both write job_type 'refresh'. The dashboard must
    show each its OWN latest run, not the other's, via the stamped sched_id."""
    _seed_run(db_session, "refresh", minutes_ago=30, sched_id="refresh")
    stuck = _seed_run(db_session, "refresh", minutes_ago=1440, sched_id="refresh_stuck")
    now = datetime.now(UTC)

    refresh_job = admin_router._scheduler_job(db_session, _entry("refresh"), now, drill=False)
    stuck_job = admin_router._scheduler_job(db_session, _entry("refresh_stuck"), now, drill=False)

    assert refresh_job.last_run.stats["sched_id"] == "refresh"
    assert stuck_job.last_run.id == stuck.id
    assert stuck_job.last_run.stats["sched_id"] == "refresh_stuck"


def test_untagged_legacy_runs_still_resolve_by_job_type(db_session):
    # A run predating the sched_id stamp (no sched_id in stats) still shows up — the
    # join falls back to the plain latest when nothing of that job_type is tagged.
    row = _seed_run(db_session, "refresh", minutes_ago=30)  # no sched_id
    now = datetime.now(UTC)
    job = admin_router._scheduler_job(db_session, _entry("refresh"), now, drill=False)
    assert job.last_run.id == row.id


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
