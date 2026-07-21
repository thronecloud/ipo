"""Admin 'Engine Room' endpoints — overview, jobs, coverage, personas, usage, run."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from api.deps import PERSONA_ORDER, TOTAL_PERSONAS, get_db, persona_meta
from api.schemas import (
    AdminOverview,
    CoverageRow,
    DataQualityOverview,
    DimensionCoverage,
    DQDiscrepancy,
    DQStockRow,
    Freshness,
    JobRow,
    JobRunRequest,
    JobRunResponse,
    ModelUsage,
    PersonaDistribution,
    SchedulerJob,
    SchedulerOverview,
    Usage,
)
from db.models import (
    Analysis,
    CompositeScore,
    DataQualityReport,
    JobRun,
    Stock,
    StockSnapshot,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])

# Jobs that map to `python -m engine.run <job>`.
ALLOWED_JOBS = {"discover", "refresh", "enrich", "analyze", "score", "status",
                "backfill", "dq_audit", "dq_fill"}
ANALYZE_MAX_LIMIT = 25  # refuse a bare analyze; require a small limit


def _duration_s(started, finished) -> float | None:
    if started and finished:
        return round((finished - started).total_seconds(), 1)
    return None


def _job_row(j: JobRun) -> JobRow:
    return JobRow(
        id=j.id,
        job_type=j.job_type,
        target=j.target,
        status=j.status,
        started_at=j.started_at,
        finished_at=j.finished_at,
        duration_s=_duration_s(j.started_at, j.finished_at),
        stats=j.stats,
        error=j.error,
    )


# ---- coverage helpers ----------------------------------------------------

def _analyzed_stock_ids(db: Session) -> set[int]:
    return {r for (r,) in db.execute(select(distinct(CompositeScore.stock_id))).all()}


def _full_data_stock_ids(db: Session) -> set[int]:
    return {
        r for (r,) in db.execute(
            select(distinct(StockSnapshot.stock_id)).where(
                StockSnapshot.source == "yfinance",
                StockSnapshot.data_quality == "full",
            )
        ).all()
    }


def _coverage_rows(db: Session) -> list[CoverageRow]:
    analyzed = _analyzed_stock_ids(db)
    full = _full_data_stock_ids(db)
    tracked: dict[str, int] = {}
    full_by_uni: dict[str, int] = {}
    analyzed_by_uni: dict[str, int] = {}
    for sid, uni in db.execute(select(Stock.id, Stock.universe)).all():
        for tag in uni or []:
            tracked[tag] = tracked.get(tag, 0) + 1
            if sid in full:
                full_by_uni[tag] = full_by_uni.get(tag, 0) + 1
            if sid in analyzed:
                analyzed_by_uni[tag] = analyzed_by_uni.get(tag, 0) + 1
    rows = []
    for tag in sorted(tracked, key=lambda t: -tracked[t]):
        t = tracked[tag]
        a = analyzed_by_uni.get(tag, 0)
        rows.append(
            CoverageRow(
                universe=tag,
                tracked=t,
                full_data=full_by_uni.get(tag, 0),
                analyzed=a,
                backlog=t - a,
            )
        )
    return rows


# ---- endpoints -----------------------------------------------------------

@router.get("/overview", response_model=AdminOverview)
def overview(db: Session = Depends(get_db)):
    stocks_total = db.scalar(select(func.count(Stock.id))) or 0
    by_status = dict(
        db.execute(select(Stock.status, func.count()).group_by(Stock.status)).all()
    )
    snap_counts = dict(
        db.execute(select(StockSnapshot.source, func.count()).group_by(StockSnapshot.source)).all()
    )
    snapshots = {
        "yfinance": snap_counts.get("yfinance", 0),
        "screener": snap_counts.get("screener", 0),
    }
    analyses_total = db.scalar(select(func.count(Analysis.id))) or 0
    scored = db.scalar(select(func.count(distinct(CompositeScore.stock_id)))) or 0

    coverage = _coverage_rows(db)
    freshness = _freshness(db)

    recent_jobs = [
        _job_row(j)
        for j in db.scalars(select(JobRun).order_by(JobRun.id.desc()).limit(5)).all()
    ]

    return AdminOverview(
        stocks_total=stocks_total,
        by_status=by_status,
        snapshots=snapshots,
        analyses_total=analyses_total,
        scored=scored,
        analysis_backlog=max(stocks_total - scored, 0),
        coverage=coverage,
        freshness=freshness,
        recent_jobs=recent_jobs,
    )


def _freshness(db: Session) -> Freshness:
    """Bucket stocks by the age of their latest snapshot."""
    sq = (
        select(
            StockSnapshot.stock_id,
            func.max(StockSnapshot.captured_at).label("latest"),
        )
        .group_by(StockSnapshot.stock_id)
        .subquery()
    )
    now = datetime.now(timezone.utc)
    fresh_cut = now - timedelta(hours=24)
    stale_cut = now - timedelta(days=7)
    fresh = stale = older = 0
    for (_, latest) in db.execute(select(sq.c.stock_id, sq.c.latest)).all():
        if latest is None:
            older += 1
        elif latest >= fresh_cut:
            fresh += 1
        elif latest >= stale_cut:
            stale += 1
        else:
            older += 1
    return Freshness(fresh_24h=fresh, stale_7d=stale, older=older)


@router.get("/jobs", response_model=list[JobRow])
def jobs(
    db: Session = Depends(get_db),
    limit: int = 50,
    type: str | None = None,
):
    q = select(JobRun).order_by(JobRun.id.desc())
    if type:
        q = q.where(JobRun.job_type == type)
    q = q.limit(max(1, min(limit, 500)))
    return [_job_row(j) for j in db.scalars(q).all()]


@router.get("/coverage", response_model=list[CoverageRow])
def coverage(db: Session = Depends(get_db)):
    return _coverage_rows(db)


@router.get("/data-quality", response_model=DataQualityOverview)
def data_quality(db: Session = Depends(get_db)):
    """Latest data-quality picture: rollup from the last dq_audit run + the current
    worst offenders and cross-source discrepancy flags (from the latest report/stock)."""
    # Rollup from the most recent dq_audit JobRun (audit already computed it).
    last = db.scalar(
        select(JobRun)
        .where(JobRun.job_type == "dq_audit", JobRun.status == "success")
        .order_by(JobRun.id.desc())
        .limit(1)
    )
    stats = (last.stats if last else None) or {}
    dim_cov = {
        k: DimensionCoverage(**v) for k, v in (stats.get("dimension_coverage") or {}).items()
    }

    # Latest report per stock (DISTINCT ON) joined to identity.
    sq = (
        select(
            DataQualityReport.stock_id,
            DataQualityReport.overall_score,
            DataQualityReport.grade,
            DataQualityReport.flags,
            DataQualityReport.missing,
        )
        .order_by(DataQualityReport.stock_id, DataQualityReport.checked_at.desc(), DataQualityReport.id.desc())
        .distinct(DataQualityReport.stock_id)
        .subquery()
    )
    rows = db.execute(
        select(Stock.symbol, Stock.company_name, sq.c.overall_score, sq.c.grade,
               sq.c.flags, sq.c.missing)
        .join(sq, sq.c.stock_id == Stock.id)
    ).all()

    worst: list[DQStockRow] = []
    discrepancies: list[DQDiscrepancy] = []
    scored_rows = []
    for sym, name, overall, grade, flags, missing in rows:
        flag_types = [f.get("type") for f in (flags or [])]
        if overall is not None:
            scored_rows.append((sym, name, overall, grade, flag_types, missing or []))
        for f in (flags or []):
            if str(f.get("type", "")).endswith("_mismatch"):
                discrepancies.append(DQDiscrepancy(symbol=sym, type=f["type"], detail=f.get("detail", "")))

    scored_rows.sort(key=lambda r: r[2])  # lowest overall first
    for sym, name, overall, grade, flag_types, missing in scored_rows[:25]:
        worst.append(DQStockRow(symbol=sym, company_name=name, overall_score=overall,
                                grade=grade, flags=flag_types, missing=missing))

    return DataQualityOverview(
        audited_at=last.finished_at if last else None,
        audited=stats.get("audited", len(rows)),
        avg_overall=stats.get("avg_overall"),
        grades=stats.get("grades", {}),
        dimension_coverage=dim_cov,
        flags=stats.get("flags", {}),
        missing=stats.get("missing", {}),
        worst=worst,
        discrepancies=discrepancies[:50],
    )


@router.get("/personas/distribution", response_model=list[PersonaDistribution])
def personas_distribution(db: Session = Depends(get_db)):
    # Latest analysis per (stock, persona).
    sq = (
        select(Analysis.persona, Analysis.score, Analysis.recommendation)
        .order_by(Analysis.stock_id, Analysis.persona, Analysis.analyzed_at.desc(), Analysis.id.desc())
        .distinct(Analysis.stock_id, Analysis.persona)
        .subquery()
    )
    rows = db.execute(select(sq.c.persona, sq.c.score, sq.c.recommendation)).all()

    agg: dict[str, dict] = {
        slug: {"count": 0, "sum": 0, "n_scored": 0, "hist": [0] * 11,
               "rec": {"BUY": 0, "HOLD": 0, "AVOID": 0}}
        for slug in PERSONA_ORDER
    }
    for persona, score, rec in rows:
        a = agg.setdefault(
            persona,
            {"count": 0, "sum": 0, "n_scored": 0, "hist": [0] * 11,
             "rec": {"BUY": 0, "HOLD": 0, "AVOID": 0}},
        )
        a["count"] += 1
        if score is not None and 0 <= score <= 10:
            a["sum"] += score
            a["n_scored"] += 1
            a["hist"][score] += 1
        if rec in a["rec"]:
            a["rec"][rec] += 1

    out = []
    for slug in PERSONA_ORDER:
        a = agg[slug]
        out.append(
            PersonaDistribution(
                persona=slug,
                display_name=persona_meta(slug)["display_name"],
                count=a["count"],
                avg_score=round(a["sum"] / a["n_scored"], 2) if a["n_scored"] else None,
                score_hist=a["hist"],
                rec_counts=a["rec"],
            )
        )
    return out


@router.get("/usage", response_model=Usage)
def usage(db: Session = Depends(get_db)):
    by_model: dict[str, ModelUsage] = {}
    rows = db.execute(
        select(Analysis.model, Analysis.total_cost_usd, Analysis.usage)
    ).all()
    acc: dict[str, dict] = {}
    for model, cost, use in rows:
        m = model or "unknown"
        a = acc.setdefault(m, {"analyses": 0, "cost": 0.0, "in": 0, "out": 0})
        a["analyses"] += 1
        a["cost"] += cost or 0.0
        use = use or {}
        # True input volume = fresh input + cache writes + cache reads
        # (the CLI reports these separately; summing only input_tokens undercounts badly).
        a["in"] += (
            int(use.get("input_tokens") or 0)
            + int(use.get("cache_creation_input_tokens") or 0)
            + int(use.get("cache_read_input_tokens") or 0)
        )
        a["out"] += int(use.get("output_tokens") or 0)
    for m, a in acc.items():
        by_model[m] = ModelUsage(
            analyses=a["analyses"],
            total_cost_usd=round(a["cost"], 4),
            input_tokens=a["in"],
            output_tokens=a["out"],
        )

    total_analyses = db.scalar(select(func.count(Analysis.id))) or 0
    stocks_total = db.scalar(select(func.count(Stock.id))) or 0
    scored = db.scalar(select(func.count(distinct(CompositeScore.stock_id)))) or 0

    return Usage(
        by_model=by_model,
        total_analyses=total_analyses,
        backlog=max(stocks_total - scored, 0),
    )


# ---- scheduler (Engine Room) ---------------------------------------------

def _latest_run(db: Session, job_type: str) -> JobRun | None:
    return db.scalar(
        select(JobRun)
        .where(JobRun.job_type == job_type)
        .order_by(JobRun.id.desc())
        .limit(1)
    )


def _next_after(trigger, last: datetime) -> datetime | None:
    """The first scheduled fire strictly after `last`. For interval triggers this
    is last+interval (ignoring jitter); for cron, apscheduler's own arithmetic."""
    from apscheduler.triggers.interval import IntervalTrigger

    if isinstance(trigger, IntervalTrigger):
        return last + trigger.interval
    return trigger.get_next_fire_time(last, last)


def _cadence_grace(trigger, ref: datetime) -> timedelta:
    """One nominal cadence period of slack before a skipped run counts as missed —
    i.e. the job has to miss a whole cycle, not merely run a little late."""
    from apscheduler.triggers.interval import IntervalTrigger

    if isinstance(trigger, IntervalTrigger):
        return trigger.interval
    t1 = trigger.get_next_fire_time(None, ref)
    if t1 is None:
        return timedelta(hours=6)
    t2 = trigger.get_next_fire_time(t1, t1)
    return (t2 - t1) if t2 else timedelta(hours=6)


def _scheduler_job(db: Session, entry, now: datetime, drill: bool) -> SchedulerJob:
    from apscheduler.triggers.interval import IntervalTrigger

    kind = "interval" if isinstance(entry.trigger, IntervalTrigger) else "cron"

    # Untracked (reap): writes no job_run, so there is nothing to join against.
    if entry.job_type is None:
        return SchedulerJob(
            id=entry.id, description=entry.description, job_type=None,
            cadence=entry.cadence, trigger_kind=kind,
            last_run=None,
            next_expected=entry.trigger.get_next_fire_time(None, now),
            missed=None, recent_runs=[],
        )

    last = _latest_run(db, entry.job_type)
    if last is None:
        # Declared and tracked, but no run on record — overdue by definition.
        next_expected = entry.trigger.get_next_fire_time(None, now)
        missed = True
    else:
        next_expected = _next_after(entry.trigger, last.started_at)
        grace = _cadence_grace(entry.trigger, last.started_at)
        missed = bool(next_expected and now > next_expected + grace)

    recent: list = []
    if drill:
        recent = [
            _job_row(j)
            for j in db.scalars(
                select(JobRun)
                .where(JobRun.job_type == entry.job_type)
                .order_by(JobRun.id.desc())
                .limit(10)
            ).all()
        ]

    return SchedulerJob(
        id=entry.id, description=entry.description, job_type=entry.job_type,
        cadence=entry.cadence, trigger_kind=kind,
        last_run=_job_row(last) if last else None,
        next_expected=next_expected, missed=missed, recent_runs=recent,
    )


@router.get("/scheduler", response_model=SchedulerOverview)
def scheduler(db: Session = Depends(get_db), job: str | None = None):
    """Join the declared schedule (expected cadence + next fire) against the
    job_runs history (last run, status, stats, error) so the Engine Room can show,
    per job, when it was supposed to run, whether it did, and how it ended. Pass
    ?job=<id> to also get that job's last 10 runs."""
    from engine.scheduler import schedule_manifest

    now = datetime.now(timezone.utc)
    jobs = [
        _scheduler_job(db, entry, now, drill=(job == entry.id))
        for entry in schedule_manifest()
    ]
    return SchedulerOverview(now=now, jobs=jobs)


# ---- job launcher --------------------------------------------------------

def _authorize(request: Request, x_admin_token: str | None):
    """Allow if ADMIN_TOKEN matches, else only localhost."""
    expected = os.environ.get("ADMIN_TOKEN")
    if expected:
        if x_admin_token != expected:
            raise HTTPException(status_code=401, detail="Invalid or missing X-Admin-Token")
        return
    # No token configured -> localhost-only.
    client_host = request.client.host if request.client else None
    if client_host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(
            status_code=403,
            detail="Admin job runs restricted to localhost (set ADMIN_TOKEN to allow remote)",
        )


@router.post("/jobs/run", response_model=JobRunResponse)
def run_job(
    body: JobRunRequest,
    request: Request,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    _authorize(request, x_admin_token)

    job = (body.job or "").strip()
    if job not in ALLOWED_JOBS:
        raise HTTPException(status_code=400, detail=f"Unknown job '{job}'. Allowed: {sorted(ALLOWED_JOBS)}")

    args = body.args or {}

    # Guard: analyze must specify a small limit (it spends Fable/Max limits).
    if job == "analyze":
        limit = args.get("limit")
        if not isinstance(limit, int) or limit <= 0 or limit > ANALYZE_MAX_LIMIT:
            raise HTTPException(
                status_code=400,
                detail=f"'analyze' requires an integer 'limit' between 1 and {ANALYZE_MAX_LIMIT}",
            )

    cmd = [sys.executable, "-m", "engine.run", job]
    for key, val in args.items():
        if val is None or val is False:
            continue
        flag = f"--{key.replace('_', '-')}"
        if val is True:
            cmd.append(flag)
        elif isinstance(val, (list, tuple)):
            cmd.append(flag)
            cmd.extend(str(v) for v in val)
        else:
            cmd.extend([flag, str(val)])

    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    # Non-blocking, detached from the API process group.
    subprocess.Popen(
        cmd,
        cwd=project_root,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    return JobRunResponse(launched=True, job=job)
