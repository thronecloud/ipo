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

Production hardening:
  - every tick is exception-isolated (one bad tick never kills the daemon);
    failures are also recorded in job_runs by the engine's job_run wrapper.
  - jobs coalesce and never run concurrently with themselves.

Run:  python -m engine.scheduler   (host or container — see docker-compose.yml)
"""

import os
import traceback
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import func, select

from engine.analysis.engine import run_incremental
from engine.ingest.amfi import auto_reingest
from engine.ingest.discover import discover_ipos
from engine.ingest.screener_enrich import enrich
from engine.ingest.upcoming import promote_listed, register_upcoming
from engine.ingest.yf_refresh import refresh


def _int(env, default):
    try:
        return int(os.environ.get(env, default))
    except ValueError:
        return default


# Bounded batch sizes so a single tick can't blow the usage budget.
ANALYZE_BATCH = _int("SCHED_ANALYZE_BATCH", 20)
REFRESH_BATCH = _int("SCHED_REFRESH_BATCH", 100)
ENRICH_BATCH = _int("SCHED_ENRICH_BATCH", 50)
# Hard ceiling on persona analyses per UTC day (Max-plan protection).
ANALYZE_DAILY_CAP = _int("SCHED_ANALYZE_DAILY_CAP", 200)


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
            try:
                from engine.notify import notify
                notify(f"scheduler tick failed: {job_fn.__name__}",
                       str(e)[:400], priority="high", tags="rotating_light")
            except Exception:
                pass

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
    from db.base import SessionLocal
    from db.models import Analysis

    midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    session = SessionLocal()
    try:
        return session.scalar(
            select(func.count()).select_from(Analysis).where(Analysis.analyzed_at >= midnight)
        ) or 0
    finally:
        session.close()


def job_discover():
    # APPEND-ONLY: scrapes screener's recent-IPO front pages and registers any
    # newly-listed company. Never mutates or removes existing stocks.
    print(f"[scheduler][{_now()}] discover (append-only)")
    discover_ipos(year=None, pages=3)


def job_refresh():
    print(f"[scheduler][{_now()}] refresh (batch={REFRESH_BATCH})")
    refresh(limit=REFRESH_BATCH, verbose=False)


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


def build_scheduler() -> BlockingScheduler:
    # Default to UTC so schedules are unambiguous across hosts.
    sched = BlockingScheduler(
        timezone=os.environ.get("SCHED_TZ", "UTC"),
        job_defaults={
            "coalesce": True,          # collapse missed runs into one
            "max_instances": 1,        # a job never overlaps itself
            "misfire_grace_time": 3600,
        },
    )
    # Daily upcoming-IPO check at 13:30 UTC — after Indian market close, before
    # the 14:00 listed-discovery so promotion's twin-merge sees prior discoveries.
    sched.add_job(safe(job_upcoming), CronTrigger(hour=13, minute=30), id="upcoming")
    # Daily discovery at 14:00 UTC — APPEND-ONLY (adds new companies).
    sched.add_job(safe(job_discover), CronTrigger(hour=14, minute=0), id="discover")
    # Daily data refresh at 02:00 UTC (hash-gated; snapshot added only when data changed).
    sched.add_job(safe(job_refresh), CronTrigger(hour=2, minute=0), id="refresh")
    # Weekly screener enrichment (fundamentals move quarterly) — Sunday 03:00 UTC.
    sched.add_job(safe(job_enrich), CronTrigger(day_of_week="sun", hour=3, minute=0), id="enrich")
    # Monthly AMFI release check (new Jan/Jul reclassifications auto-ingest) — 5th, 04:00 UTC.
    sched.add_job(safe(job_amfi), CronTrigger(day=5, hour=4, minute=0), id="amfi")
    # Hourly analysis batch drains the backlog over time (credential- and cap-gated).
    sched.add_job(safe(job_analyze_and_score), CronTrigger(minute=30), id="analyze")
    return sched


def main():
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
