"""
Freshness SLOs — per-dataset staleness contracts, declared as data.

Each dataset has a freshness target expressed against the relevant population
(e.g. "the newest price bar of every ACTIVE stock is at most one trading day
old"). `compute_slos()` measures the population in a handful of set-based
grouped scans — never per-stock N+1 — and reports, per dataset:

  - compliance %      : share of the population inside its target
  - worst offenders   : the members furthest past it (or that were never fetched)

Trading-day lag (price bars, index bars) is measured in TRADING days, not
calendar days: a bar from Friday read on Saturday is zero trading days stale,
not one. The trading calendar is the union of the dates the data itself shows
the market traded (weekends and historical holidays are simply absent),
extended forward from the last known day with plain weekday logic so an OUTAGE
past the last bar still counts as stale instead of masking itself.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from db.models import (
    CorporateAnnouncement,
    DailyPrice,
    IndexPrice,
    JobRun,
    ShareholdingPattern,
    Stock,
    StockSnapshot,
)

# Shareholding is quarterly and filed ~45 days after quarter-end, so the newest
# pattern is expected within one quarter plus that filing grace.
QUARTER_DAYS = 91
SHAREHOLDING_GRACE_DAYS = 45

# How many worst offenders to surface per dataset.
TOP_OFFENDERS = 10


@dataclass(frozen=True)
class SloSpec:
    dataset: str            # stable machine key
    description: str        # human sentence
    target: float           # numeric threshold, in `unit`
    unit: str               # "trading_days" | "days"
    target_text: str        # human target, e.g. "≤ 1 trading day"
    objective_pct: float = 100.0   # compliance below this reads as breached
    provisional: bool = False      # collector known-broken → render neutral (grey),
    #                                not red; the freshness number is not yet a
    #                                contract we hold the pipeline to.


# The snapshot target is set to what the refresh cadence can actually deliver, not
# to an aspiration. The active universe is ~2.5k stocks; yfinance throttles a nightly
# refresh to ~100-150 viable snapshots per run, so a full pass over the universe takes
# ~20 nights. Measured on prod (2026-07): a ≤7d or ≤14d target sits at 3.6% compliance,
# while ≤21d clears 100% — the population's oldest snapshot is ~18d. 21 days (three
# weeks) is thus the tightest target the ~20-night refresh cycle satisfies with slack
# for throttled nights; anything tighter is permanently red by construction, not by
# any real staleness the cadence could fix.
SNAPSHOT_TARGET_DAYS = 21

SLO_SPECS: list[SloSpec] = [
    SloSpec("price_bars", "Newest daily price bar per active stock",
            1, "trading_days", "≤ 1 trading day"),
    SloSpec("announcements", "Newest corporate announcement across the feed",
            1, "days", "≤ 1 day"),
    SloSpec("snapshots", "Newest yfinance snapshot per active stock",
            SNAPSHOT_TARGET_DAYS, "days", "≤ 21 days"),
    SloSpec("index_prices", "Newest bar per benchmark index",
            1, "trading_days", "≤ 1 trading day"),
    SloSpec("shareholding", "Newest shareholding pattern per active stock (that has one)",
            QUARTER_DAYS + SHAREHOLDING_GRACE_DAYS, "days", "≤ quarter + 45d",
            provisional=True),
    SloSpec("dq_audit", "Recency of the last successful data-quality audit",
            2, "days", "≤ 2 days"),
]


# ---- trading-day arithmetic ---------------------------------------------

def _trading_calendar(known: list[date], today: date) -> list[date]:
    """Sorted trading days: the distinct dates the data shows the market traded,
    extended forward from the last known day with weekday logic up to `today`.

    The forward synthesis is what stops an outage from hiding: if bars stopped on
    a Wednesday but the market traded Thu/Fri, those weekdays are added even though
    no bar exists for them, so the lag reflects the gap. Weekends are never added,
    so a Friday bar read on a Saturday/Sunday stays zero trading days stale."""
    cal = sorted(set(known))
    d = (cal[-1] + timedelta(days=1)) if cal else today
    while d <= today:
        if d.weekday() < 5:   # Mon-Fri
            cal.append(d)
        d += timedelta(days=1)
    return cal


def _trading_lag(bar_date: date, cal: list[date], today: date) -> int:
    """Trading days between `bar_date` and the latest trading day on/before today."""
    hi = bisect.bisect_right(cal, today) - 1
    if hi < 0:
        return 0
    j = bisect.bisect_right(cal, bar_date) - 1
    if j < 0:
        return hi + 1          # the bar predates the whole calendar
    return max(hi - j, 0)


def _age_days(ts: datetime, now: datetime) -> float:
    """Calendar-day age of a timestamp, tolerant of a naive value from the driver."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return round((now - ts).total_seconds() / 86400, 2)


# ---- per-dataset population builders (grouped scans, never N+1) ----------
# Each returns [(label, lag_or_None)]: lag None means the member was never
# fetched (the worst kind of offender).

def _active_stocks(session: Session) -> list[tuple[int, str]]:
    return session.execute(
        select(Stock.id, Stock.symbol).where(Stock.status == "active")
    ).all()


def _price_bars(session: Session, now: datetime) -> list[tuple[str, int | None]]:
    known = list(session.scalars(select(distinct(DailyPrice.date))).all())
    cal = _trading_calendar(known, now.date())
    newest = dict(
        session.execute(
            select(DailyPrice.stock_id, func.max(DailyPrice.date))
            .join(Stock, Stock.id == DailyPrice.stock_id)
            .where(Stock.status == "active")
            .group_by(DailyPrice.stock_id)
        ).all()
    )
    return [
        (sym, None if newest.get(sid) is None else _trading_lag(newest[sid], cal, now.date()))
        for sid, sym in _active_stocks(session)
    ]


def _index_prices(session: Session, now: datetime) -> list[tuple[str, int | None]]:
    known = list(session.scalars(select(distinct(IndexPrice.date))).all())
    cal = _trading_calendar(known, now.date())
    rows = session.execute(
        select(IndexPrice.symbol, func.max(IndexPrice.date)).group_by(IndexPrice.symbol)
    ).all()
    return [(sym, _trading_lag(d, cal, now.date())) for sym, d in rows]


def _snapshots(session: Session, now: datetime) -> list[tuple[str, float | None]]:
    newest = dict(
        session.execute(
            select(StockSnapshot.stock_id, func.max(StockSnapshot.captured_at))
            .join(Stock, Stock.id == StockSnapshot.stock_id)
            .where(Stock.status == "active", StockSnapshot.source == "yfinance")
            .group_by(StockSnapshot.stock_id)
        ).all()
    )
    return [
        (sym, None if newest.get(sid) is None else _age_days(newest[sid], now))
        for sid, sym in _active_stocks(session)
    ]


def _shareholding(session: Session, now: datetime) -> list[tuple[str, float | None]]:
    # Population = active stocks that HAVE a pattern (inner join): a stock the
    # feed never covered is out of scope, not an offender.
    rows = session.execute(
        select(Stock.symbol, func.max(ShareholdingPattern.period_end))
        .join(ShareholdingPattern, ShareholdingPattern.stock_id == Stock.id)
        .where(Stock.status == "active")
        .group_by(Stock.id, Stock.symbol)
    ).all()
    today = now.date()
    return [(sym, float((today - pe).days)) for sym, pe in rows]


def _announcements(session: Session, now: datetime) -> list[tuple[str, float | None]]:
    newest = session.scalar(select(func.max(CorporateAnnouncement.announced_at)))
    return [("newest announcement", None if newest is None else _age_days(newest, now))]


def _dq_audit(session: Session, now: datetime) -> list[tuple[str, float | None]]:
    last = session.scalar(
        select(func.max(JobRun.finished_at)).where(
            JobRun.job_type == "dq_audit",
            JobRun.status.in_(("success", "partial")),
        )
    )
    return [("last dq_audit", None if last is None else _age_days(last, now))]


_BUILDERS = {
    "price_bars": _price_bars,
    "announcements": _announcements,
    "snapshots": _snapshots,
    "index_prices": _index_prices,
    "shareholding": _shareholding,
    "dq_audit": _dq_audit,
}


# ---- summary -------------------------------------------------------------

def _offender_key(item: tuple[str, float | None]):
    """Sort key: never-fetched first, then descending lag."""
    _, lag = item
    return (0, 0.0) if lag is None else (1, -lag)


def _summarize(spec: SloSpec, members: list[tuple[str, float | None]]) -> dict:
    population = len(members)
    compliant = sum(1 for _, lag in members if lag is not None and lag <= spec.target)
    lags = [lag for _, lag in members if lag is not None]
    worst_lag = max(lags) if lags else None
    missing = sum(1 for _, lag in members if lag is None)

    offenders = sorted(
        [(label, lag) for label, lag in members if lag is None or lag > spec.target],
        key=_offender_key,
    )[:TOP_OFFENDERS]

    compliance_pct = round(100 * compliant / population, 1) if population else None
    # A provisional dataset (collector known-broken) is never "breached": its
    # staleness is a plumbing outage we already know about, so it must read neutral
    # (grey), never red — a red here would be crying wolf, not telling the truth.
    breached = (not spec.provisional
                and compliance_pct is not None
                and compliance_pct < spec.objective_pct)

    return {
        "dataset": spec.dataset,
        "description": spec.description,
        "target": spec.target_text,
        "unit": spec.unit,
        "objective_pct": spec.objective_pct,
        "provisional": spec.provisional,
        "population": population,
        "compliant": compliant,
        "compliance_pct": compliance_pct,
        "worst_lag": worst_lag,
        "missing": missing,
        "breached": breached,
        "offenders": [
            {"label": label, "lag": lag, "missing": lag is None}
            for label, lag in offenders
        ],
    }


def compute_slos(session: Session, now: datetime | None = None) -> list[dict]:
    """Every declared SLO, measured against current data. Pure read; safe to call
    from the API on every scheduler-overview request."""
    now = now or datetime.now(timezone.utc)
    return [_summarize(spec, _BUILDERS[spec.dataset](session, now)) for spec in SLO_SPECS]
