"""
The living-engine scheduler — an always-on process that keeps data fresh and
analysis current, without anyone running a script by hand.

Cadence (UTC, all configurable via env):
  - discover  daily 14:00 : find new IPOs on screener (APPEND-ONLY)
  - refresh   daily 02:00 : re-pull yfinance for active universe (hash-gated)
  - enrich    Sun   03:00 : screener fundamentals batch (hash-gated)
  - analyze   hourly :30  : drain the analysis backlog in bounded batches,
                            gated by credential availability + a DAILY CAP so
                            an unattended scheduler can never burn the Max plan.
  - reap      hourly :10  : error out job_runs stranded at 'running' by a restart

Production hardening:
  - every tick is exception-isolated (one bad tick never kills the daemon);
    failures are also recorded in job_runs by the engine's job_run wrapper.
  - jobs coalesce and never run concurrently with themselves.

Run:  python -m engine.scheduler   (host or container — see docker-compose.yml)
"""

import os
import traceback
from collections import namedtuple
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from engine.analysis.engine import run_incremental
from engine.ingest.amfi import auto_reingest
from engine.ingest.discover import discover_ipos
from engine.ingest.screener_enrich import enrich
from engine.ingest.index_prices import refresh_index_prices
from engine.ingest.upcoming import promote_listed, register_upcoming
from engine.ingest.yf_refresh import refresh
from engine.quality.audit import audit
from engine.quality.gapfill import gapfill
from engine.repo import job_run, reconcile_scores


def _int(env, default):
    try:
        return int(os.environ.get(env, default))
    except ValueError:
        return default


# Bounded batch sizes so a single tick can't blow the usage budget.
ANALYZE_BATCH = _int("SCHED_ANALYZE_BATCH", 20)
REFRESH_BATCH = _int("SCHED_REFRESH_BATCH", 100)
ENRICH_BATCH = _int("SCHED_ENRICH_BATCH", 50)
# Dedicated daily price rotation (problem: the snapshot refresh only reaches its
# own batch, so most of the 2400-stock universe carries bars weeks old). This
# pass uses the snapshot-decoupled backfill, walks the FULL active universe in
# least-recently-priced order, and runs often enough that the whole universe is
# covered inside two trading days. Chunked so one run stays polite to yfinance.
PRICE_BATCH = _int("SCHED_PRICE_BATCH", 220)
PRICE_INTERVAL_HOURS = _int("SCHED_PRICE_INTERVAL_HOURS", 2)
# Hard ceiling on persona analyses per UTC day (Max-plan protection).
ANALYZE_DAILY_CAP = _int("SCHED_ANALYZE_DAILY_CAP", 200)
# Bounded gap-fill sweep per weekly tick (identity is free; network parts capped).
DQ_FILL_BATCH = _int("SCHED_DQ_FILL_BATCH", 150)
# Grace window before a still-"running" job_run is treated as dead. The api
# container launches detached engine jobs of its own, so a scheduler restart must
# not error work that is genuinely in flight elsewhere. The slowest bounded batch
# is 25 analyze pairs x the 300s backend cap ~= 2h; 24h clears that by an order of
# magnitude while still catching zombies, which otherwise persist forever.
REAP_ORPHAN_HOURS = _int("SCHED_REAP_ORPHAN_HOURS", 24)

# Misfire grace: how long after a missed trigger APScheduler may still run a job.
# Daily jobs get hours (a container down through 02:00 still runs when it wakes at
# 04:00); the hourly ticks get minutes (a stale hourly run is pointless — the next
# hour's is due anyway). Beyond the grace window, the startup catch-up below is the
# backstop so a whole day's daily run is never silently skipped.
MISFIRE_DAILY = _int("SCHED_MISFIRE_DAILY", 6 * 3600)
MISFIRE_HOURLY = _int("SCHED_MISFIRE_HOURLY", 300)
# Slack added to a daily job's cadence before the boot catch-up considers it
# overdue — absorbs normal jitter so a run a few hours late isn't re-fired.
CATCHUP_SLACK_HOURS = _int("SCHED_CATCHUP_SLACK_HOURS", 6)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def safe(job_fn):
    """Exception-isolate a tick: log loudly, never kill the daemon."""

    def wrapper():
        try:
            job_fn()
        except Exception as e:
            print(f"[scheduler][{_now()}] TICK FAILED in {job_fn.__name__}:")
            traceback.print_exc()
            from engine.notify import notify_safe
            notify_safe(f"scheduler tick failed: {job_fn.__name__}",
                        str(e)[:400], priority="high", tags="rotating_light")

    wrapper.__name__ = job_fn.__name__
    return wrapper


def analysis_available() -> bool:
    """True when the Claude CLI has a usable credential in this environment.

    Headless/container: CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`).
    Host: an interactive `claude` login (~/.claude exists).
    """
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return True
    if os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("ANALYSIS_BACKEND") == "api":
        return True
    return Path.home().joinpath(".claude").exists()


def analyses_done_today() -> int:
    """Backend attempts made today (successes + errors), against the daily cap.

    Counting only successful Analysis rows let a rate-limited day burn the plan:
    every failed attempt left the count at zero, the cap never advanced, and all
    24 hourly ticks fired straight into the limit. An outage is exactly when the
    cap must engage — so count what every tick actually spent: success + error
    from today's analyze job_runs, where a failed attempt costs as much as a
    successful one."""
    from db.base import SessionLocal
    from db.models import JobRun

    midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    session = SessionLocal()
    try:
        rows = session.scalars(
            select(JobRun.stats).where(
                JobRun.job_type == "analyze",
                JobRun.started_at >= midnight,
            )
        ).all()
        return sum(
            (stats.get("success") or 0) + (stats.get("error") or 0)
            for stats in rows if isinstance(stats, dict)
        )
    finally:
        session.close()


def reap_orphaned_runs(session) -> int:
    """Mark job_runs stranded at 'running' by process death as errored.

    status='running' is only ever cleared by the job's own finally block, so a
    container restart, rebuild, or OOM kill leaves the row running forever.
    Only runs older than REAP_ORPHAN_HOURS are touched, so a job still executing
    in another container is never mistaken for a corpse.
    """
    from sqlalchemy import update

    from db.models import JobRun

    now = datetime.now(timezone.utc)
    result = session.execute(
        update(JobRun)
        .where(JobRun.status == "running",
               JobRun.started_at < now - timedelta(hours=REAP_ORPHAN_HOURS))
        .values(status="error",
                error="orphaned by process restart",
                finished_at=now)
        .execution_options(synchronize_session=False)
    )
    session.commit()
    return result.rowcount


def job_discover():
    # APPEND-ONLY: scrapes screener's recent-IPO front pages and registers any
    # newly-listed company. Never mutates or removes existing stocks.
    print(f"[scheduler][{_now()}] discover (append-only)")
    discover_ipos(year=None, pages=3)


def job_refresh():
    print(f"[scheduler][{_now()}] refresh (batch={REFRESH_BATCH})")
    refresh(limit=REFRESH_BATCH, verbose=False)


def job_refresh_stuck():
    # Weekly retry of parked stocks — a transiently-unfetchable/stale name that has
    # since recovered is auto-revived, so parking is never a permanent dead-end.
    print(f"[scheduler][{_now()}] refresh stuck (unfetchable+stale)")
    refresh(statuses=("unfetchable", "stale"), limit=REFRESH_BATCH, verbose=False)


def job_price_refresh():
    # Snapshot-decoupled price-only pass over the FULL active universe, least-
    # recently-priced first. Runs on a short interval so the whole universe is
    # repriced within a couple of trading days — the nightly snapshot refresh only
    # ever reaches its own batch, which left most stocks with weeks-old bars.
    from engine.ingest.yf_refresh import backfill_prices
    print(f"[scheduler][{_now()}] price refresh (batch={PRICE_BATCH}, oldest first)")
    backfill_prices(statuses=("active",), limit=PRICE_BATCH,
                    oldest_first=True, verbose=False)


def job_enrich():
    print(f"[scheduler][{_now()}] screener enrich (batch={ENRICH_BATCH})")
    enrich(limit=ENRICH_BATCH, verbose=False)


def job_upcoming():
    # APPEND-ONLY on real rows: registers pre-listing IPO shells (DRHP/open/close
    # stage from ipowatch), then graduates due ones into the live pipeline.
    print(f"[scheduler][{_now()}] upcoming discover + promote")
    register_upcoming(verbose=False)
    promote_listed(verbose=False)


def job_amfi():
    # Idempotent: only ingests when AMFI publishes a NEW Jan/Jul reclassification.
    print(f"[scheduler][{_now()}] amfi release check")
    auto_reingest(verbose=True)


def job_index_prices():
    # Append-only benchmark bars — the backtest's excess-return comparator
    # must never lag the stock series it is compared against.
    print(f"[scheduler][{_now()}] index prices refresh")
    refresh_index_prices(verbose=False)


def job_score():
    # Reconcile composites for stocks whose latest analysis is newer than their
    # score (heals orphans left by an interrupted analyze batch). Cheap, DB-only.
    print(f"[scheduler][{_now()}] score reconcile")
    with job_run("score_reconcile", target="stale") as (session, stats):
        stats["rescored"] = reconcile_scores(session)


def job_dq_audit():
    # Score every active stock's data quality into data_quality_reports.
    print(f"[scheduler][{_now()}] dq_audit (all active)")
    audit(verbose=False)


def job_dq_fill():
    # Close the gaps the latest audit flagged (identity free; network parts capped).
    print(f"[scheduler][{_now()}] dq_fill (batch={DQ_FILL_BATCH})")
    gapfill(limit=DQ_FILL_BATCH, verbose=False)


def job_reap():
    # A restart strands its own in-flight runs, and the boot-time reap spares them
    # for being seconds old. Recurring so those rows are cleared within the hour
    # after the grace window elapses, instead of waiting for the next restart.
    from db.base import SessionLocal

    session = SessionLocal()
    try:
        n = reap_orphaned_runs(session)
        if n:
            print(f"[scheduler][{_now()}] reaped {n} orphaned job_run(s)")
    finally:
        session.close()


def job_analyze_and_score():
    if not analysis_available():
        print(f"[scheduler][{_now()}] analyze SKIPPED — no Claude credential "
              f"(set CLAUDE_CODE_OAUTH_TOKEN via `claude setup-token` to enable)")
        return
    done = analyses_done_today()
    remaining = ANALYZE_DAILY_CAP - done
    if remaining <= 0:
        print(f"[scheduler][{_now()}] analyze SKIPPED — daily cap reached "
              f"({done}/{ANALYZE_DAILY_CAP})")
        return
    batch = min(ANALYZE_BATCH, remaining)
    print(f"[scheduler][{_now()}] analyze batch={batch} (today {done}/{ANALYZE_DAILY_CAP})")
    run_incremental(limit=batch, verbose=False)


# A daily job to catch up on boot: its job_runs `job_type`, the cadence its
# freshness is judged against, and the callable that runs it once.
CatchupSpec = namedtuple("CatchupSpec", "label job_type cadence fn")


def _catchup_specs() -> list[CatchupSpec]:
    day = timedelta(days=1)
    return [
        CatchupSpec("refresh", "refresh", day, job_refresh),
        CatchupSpec("discover", "discover_ipos", day, job_discover),
        CatchupSpec("upcoming", "discover_upcoming", day, job_upcoming),
        CatchupSpec("index_prices", "index_prices", day, job_index_prices),
        CatchupSpec("dq_audit", "dq_audit", day, job_dq_audit),
    ]


def _last_success(session, job_type: str) -> datetime | None:
    """When a job of this type last completed WITHOUT failing (success/partial).

    A run stuck at 'running' or ended in 'error' is not proof the work got done,
    so it never counts as the last-good time — an outage that errored every night
    would otherwise look fresh and suppress its own catch-up."""
    from db.models import JobRun

    return session.scalar(
        select(JobRun.started_at)
        .where(JobRun.job_type == job_type,
               JobRun.status.in_(("success", "partial")))
        .order_by(JobRun.started_at.desc())
        .limit(1)
    )


def _catchup_plan(session, now: datetime, specs) -> list[CatchupSpec]:
    """Pure decision: the daily jobs overdue for a run — never run, or whose last
    good run predates its cadence plus slack. Returned in registry order so the
    catch-up runs them in a stable, sequenced sweep."""
    slack = timedelta(hours=CATCHUP_SLACK_HOURS)
    due = []
    for spec in specs:
        last = _last_success(session, spec.job_type)
        if last is None or now - last > spec.cadence + slack:
            due.append(spec)
    return due


def run_catch_up(specs=None, now=None) -> list[str]:
    """On boot, run each overdue daily job once — SEQUENCED (one at a time, never a
    thundering herd) and observable (every job writes its own job_run, and the sweep
    itself is recorded as a `scheduler_catchup` run). Returns the labels caught up."""
    from db.base import SessionLocal

    specs = _catchup_specs() if specs is None else specs
    now = now or datetime.now(timezone.utc)

    session = SessionLocal()
    try:
        due = _catchup_plan(session, now, specs)
    finally:
        session.close()
    if not due:
        return []

    print(f"[scheduler][{_now()}] catch-up: {', '.join(s.label for s in due)}")
    with job_run("scheduler_catchup", target=",".join(s.label for s in due)) as (_s, stats):
        ran: list[str] = []
        for spec in due:
            try:
                spec.fn()                      # sequenced — the next starts only when this returns
                ran.append(spec.label)
            except Exception:
                print(f"[scheduler][{_now()}] catch-up FAILED in {spec.label}:")
                traceback.print_exc()
        stats["caught_up"] = ran
    return ran


def build_scheduler() -> BlockingScheduler:
    # Default to UTC so schedules are unambiguous across hosts.
    sched = BlockingScheduler(
        timezone=os.environ.get("SCHED_TZ", "UTC"),
        job_defaults={
            "coalesce": True,          # collapse missed runs into one
            "max_instances": 1,        # a job never overlaps itself
            "misfire_grace_time": MISFIRE_DAILY,
        },
    )
    # Daily upcoming-IPO check at 13:30 UTC — after Indian market close, before
    # the 14:00 listed-discovery so promotion's twin-merge sees prior discoveries.
    sched.add_job(safe(job_upcoming), CronTrigger(hour=13, minute=30), id="upcoming",
                  misfire_grace_time=MISFIRE_DAILY)
    # Daily discovery at 14:00 UTC — APPEND-ONLY (adds new companies).
    sched.add_job(safe(job_discover), CronTrigger(hour=14, minute=0), id="discover",
                  misfire_grace_time=MISFIRE_DAILY)
    # Daily data refresh at 02:00 UTC (hash-gated; snapshot added only when data changed).
    sched.add_job(safe(job_refresh), CronTrigger(hour=2, minute=0), id="refresh",
                  misfire_grace_time=MISFIRE_DAILY)
    # Full-universe price rotation every PRICE_INTERVAL_HOURS — least-recently-priced
    # first, so every active stock is repriced within a couple of trading days. Jitter
    # de-syncs it from the other jobs so bursts don't collide into a throttle.
    sched.add_job(safe(job_price_refresh),
                  IntervalTrigger(hours=PRICE_INTERVAL_HOURS, jitter=300),
                  id="price_refresh", misfire_grace_time=MISFIRE_HOURLY)
    # Weekly retry of parked (unfetchable/stale) stocks — Sunday 06:00 UTC. Closes the
    # dead-end: a recovered symbol auto-revives instead of needing a manual run.
    sched.add_job(safe(job_refresh_stuck), CronTrigger(day_of_week="sun", hour=6, minute=0),
                  id="refresh_stuck", misfire_grace_time=MISFIRE_DAILY)
    # Weekly screener enrichment (fundamentals move quarterly) — Sunday 03:00 UTC.
    sched.add_job(safe(job_enrich), CronTrigger(day_of_week="sun", hour=3, minute=0),
                  id="enrich", misfire_grace_time=MISFIRE_DAILY)
    # Monthly AMFI release check (new Jan/Jul reclassifications auto-ingest) — 5th, 04:00 UTC.
    sched.add_job(safe(job_amfi), CronTrigger(day=5, hour=4, minute=0), id="amfi",
                  misfire_grace_time=MISFIRE_DAILY)
    # Hourly analysis batch drains the backlog over time (credential- and cap-gated).
    sched.add_job(safe(job_analyze_and_score), CronTrigger(minute=30), id="analyze",
                  misfire_grace_time=MISFIRE_HOURLY)
    # Hourly composite reconcile at :50 — heals any composites orphaned by an
    # interrupted analyze batch (analysis and scoring never drift apart).
    sched.add_job(safe(job_score), CronTrigger(minute=50), id="score",
                  misfire_grace_time=MISFIRE_HOURLY)
    # Hourly orphan reap at :10 — the startup pass alone can never clear a run
    # stranded by the very restart that ran it (the row is seconds old and spared).
    sched.add_job(safe(job_reap), CronTrigger(minute=10), id="reap",
                  misfire_grace_time=MISFIRE_HOURLY)
    # Daily benchmark bars at 12:00 UTC — after NSE close (10:00 UTC) so the
    # day's index close is final before the evening jobs read it.
    sched.add_job(safe(job_index_prices), CronTrigger(hour=12, minute=0), id="index_prices",
                  misfire_grace_time=MISFIRE_DAILY)
    # Nightly data-quality audit at 05:00 UTC — after refresh (02:00) so it scores fresh data.
    sched.add_job(safe(job_dq_audit), CronTrigger(hour=5, minute=0), id="dq_audit",
                  misfire_grace_time=MISFIRE_DAILY)
    # Weekly gap-fill sweep — Saturday 04:00 UTC (bounded; identity fill is free).
    sched.add_job(safe(job_dq_fill), CronTrigger(day_of_week="sat", hour=4, minute=0),
                  id="dq_fill", misfire_grace_time=MISFIRE_DAILY)
    return sched


# The declared schedule, as data the Engine Room dashboard can join against the
# job_runs history. Keyed by the scheduler job id (`add_job(..., id=)`); each maps
# to a human description and the job_runs.job_type the job actually writes.
#
# The id and the job_type are NOT interchangeable: several jobs write a job_type
# that differs from their scheduler id (discover -> discover_ipos, enrich ->
# screener_enrich, amfi -> amfi_smallcap, upcoming -> discover_upcoming, score ->
# score_reconcile, price_refresh -> backfill_prices). `refresh` and `refresh_stuck`
# both write "refresh". `reap` writes NO job_run (a direct UPDATE with no wrapper),
# so its last-run cannot be tracked — job_type is None and the dashboard says so.
_JOB_META: dict[str, tuple[str, str | None]] = {
    "upcoming":      ("Discover pre-listing IPOs, then promote due ones", "discover_upcoming"),
    "discover":      ("Discover newly-listed IPOs on screener (append-only)", "discover_ipos"),
    "refresh":       ("Re-pull yfinance snapshots for the active universe", "refresh"),
    "price_refresh": ("Full-universe price rotation, least-recently-priced first", "backfill_prices"),
    "refresh_stuck": ("Weekly retry of parked (unfetchable / stale) stocks", "refresh"),
    "enrich":        ("Screener fundamentals batch (fundamentals move quarterly)", "screener_enrich"),
    "amfi":          ("AMFI Jan/Jul reclassification release check", "amfi_smallcap"),
    "analyze":       ("Drain the analysis backlog (credential- and cap-gated)", "analyze"),
    "score":         ("Reconcile composites orphaned by an interrupted analyze batch", "score_reconcile"),
    "reap":          ("Error out job_runs stranded 'running' by a restart", None),
    "index_prices":  ("Refresh benchmark index bars for the backtest comparator", "index_prices"),
    "dq_audit":      ("Score every active stock's data quality", "dq_audit"),
    "dq_fill":       ("Close the data gaps the latest audit flagged", "dq_fill"),
}

_DOW = {"mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu",
        "fri": "Fri", "sat": "Sat", "sun": "Sun"}


@dataclass(frozen=True)
class ScheduledJob:
    id: str                 # scheduler job id (add_job id=)
    description: str        # human-readable purpose
    job_type: str | None   # the job_runs.job_type it writes, or None if untracked
    cadence: str           # human trigger text, e.g. "daily 02:00 UTC" / "every 2h"
    trigger: object        # the live apscheduler trigger (for next-fire arithmetic)


def _cadence_text(trigger) -> str:
    """Render a trigger as a compact human cadence."""
    if isinstance(trigger, IntervalTrigger):
        secs = int(trigger.interval.total_seconds())
        if secs % 3600 == 0:
            return f"every {secs // 3600}h"
        if secs % 60 == 0:
            return f"every {secs // 60}m"
        return f"every {secs}s"
    f = {fld.name: str(fld) for fld in trigger.fields}
    minute = f.get("minute", "0")
    hour = f.get("hour", "*")
    if hour == "*":
        return f"hourly :{int(minute):02d}"
    hhmm = f"{int(hour):02d}:{int(minute):02d} UTC"
    dow = f.get("day_of_week", "*")
    day = f.get("day", "*")
    if dow != "*":
        return f"{_DOW.get(dow, dow)} {hhmm}"
    if day != "*":
        return f"monthly day {day} {hhmm}"
    return f"daily {hhmm}"


def schedule_manifest() -> list[ScheduledJob]:
    """The declared schedule as data — every scheduler job with its human cadence
    and the job_runs.job_type it writes.

    build_scheduler() wires the jobs into an in-memory store but starts nothing, so
    this is safe to call from the API without a running scheduler and without side
    effects. Triggers come straight from build_scheduler(), so the manifest can never
    drift from what actually runs; only the description + job_type mapping is added
    here, and a job present in build_scheduler() but missing from _JOB_META raises."""
    manifest = []
    for job in build_scheduler().get_jobs():
        try:
            description, job_type = _JOB_META[job.id]
        except KeyError:
            raise RuntimeError(
                f"scheduler job '{job.id}' has no _JOB_META entry — add its "
                f"description and job_runs.job_type so the Engine Room can show it"
            )
        manifest.append(ScheduledJob(
            id=job.id,
            description=description,
            job_type=job_type,
            cadence=_cadence_text(job.trigger),
            trigger=job.trigger,
        ))
    return manifest


def main():
    from db.base import SessionLocal

    session = SessionLocal()
    try:
        n = reap_orphaned_runs(session)
        if n:
            print(f"[scheduler][{_now()}] reaped {n} orphaned job_run(s)")
    finally:
        session.close()

    # Backstop for downtime longer than the misfire grace: run each daily job that
    # missed its window, once and sequenced, before the recurring schedule starts.
    run_catch_up()

    sched = build_scheduler()
    print(f"Living engine scheduler starting at {_now()}. Jobs:")
    for job in sched.get_jobs():
        print(f"  - {job.id}: {job.trigger}")
    print(f"  analyze: available={analysis_available()} "
          f"daily_cap={ANALYZE_DAILY_CAP} batch={ANALYZE_BATCH}")
    print("Ctrl-C to stop.")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        print("\nScheduler stopped.")


if __name__ == "__main__":
    main()
