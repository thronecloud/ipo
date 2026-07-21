"""
Cross-source reconciliation — the comparator for facts that now arrive from more than
one place.

The engine ingests the same fact from independent sources: a daily price bar lands in
`daily_prices` from the exchange bhavcopy AND from yfinance history; a market cap is
published in a yfinance snapshot AND can be computed from that snapshot's own
shares×price; promoter holding comes from the NSE shareholding-pattern feed AND from the
screener scrape. Nothing compared them. This module does — and, where they diverge beyond
the fact's tolerance, records a `source_discrepancies` row and updates a per-source-per-
fact trust score. It NEVER mutates source data: correctness is a flag for a human, not an
overwrite (the module-wide rule the DQ engine already follows).

Three comparators, each with a deliberately chosen tolerance:

  price        bhavcopy bar close  vs  the yfinance snapshot's recorded last close.
               DIVERGENCE beyond 0.5% is a discrepancy. `daily_prices` is a shared
               corrective store (last writer wins, no source column), so the bhavcopy
               side is "the stored EOD bar for that date" — on a normal trading day the
               exchange file — checked against the close yfinance itself recorded in its
               snapshot's history_summary. 0.5% sits an order of magnitude below the
               smallest genuine corporate-action rebase, so a match is real agreement and
               a miss is a real disagreement, not float noise or an intraday tick.

  market_cap   close × sharesOutstanding (both off the snapshot's info) vs the market cap
               the same snapshot publishes. Within 5% is agreement: shares outstanding and
               the quoted price are captured microseconds apart from the published cap, and
               yfinance rounds shares to the lakh, so a few percent of drift is expected;
               a bigger gap means the published cap was struck against a different share
               count (a stale float, an un-updated dilution).

  promoter_pct NSE shareholding-pattern promoter % vs the screener scrape's promoter % for
               the SAME fiscal quarter. Within 1 percentage POINT is agreement — holdings
               are reported to two decimals and the two sources round differently; a gap
               above a point is a genuinely different reading of who owns the company.

Only facts fresh enough to compare are compared: BOTH sides must be at most 7 days old,
else the pair is counted as skipped-stale (a comparison of two stale reads teaches the
trust score nothing). A run whose count of NEW discrepancies clears a threshold pages the
owner — that is a source going bad wholesale, distinct from one-off per-stock noise.

No network, no LLM, no clock beyond `as_of`. Pure read of the DB + two small writes.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from db.models import (
    DailyPrice,
    ShareholdingPattern,
    SourceDiscrepancy,
    SourceTrust,
    Stock,
    StockSnapshot,
    JobRun,
    utcnow,
)
from engine.quality.metrics import num, relative_diff
from engine.repo import job_run, latest_snapshot

# ---------- tolerances (chosen above) ----------

PRICE_DIVERGENCE_PCT = 0.5        # relative %: bhavcopy vs yfinance close
MARKET_CAP_DIVERGENCE_PCT = 5.0   # relative %: computed vs published cap
PROMOTER_DIVERGENCE_PTS = 1.0     # absolute points: NSE vs screener promoter %

# A fact is only comparable when BOTH sides were captured within this window.
FRESH_DAYS = 7
# The trailing window the rolling trust score is computed over.
TRUST_WINDOW_DAYS = 90
# A run producing more NEW discrepancies than this is a wholesale source failure, not
# per-stock noise — page the owner.
NEW_DISCREPANCY_ALERT = 25


# ---------- pure comparison core ----------

@dataclass(frozen=True)
class Comparison:
    """One resolved cross-source check: two values, their divergence, and whether they
    agree within the fact's tolerance. `divergence` is in the fact's native unit —
    relative percent for a "relative" check, absolute points for an "absolute" one."""

    fact: str
    fact_key: str
    source_a: str
    value_a: float
    source_b: str
    value_b: float
    divergence: float
    agree: bool


def compare_relative(fact, fact_key, source_a, a, source_b, b, tol_pct) -> Comparison | None:
    """Compare two values on their symmetric relative difference (in percent). Returns
    None — 'not comparable' — when either side is missing, so a one-sided fact is skipped
    rather than reported as a disagreement."""
    a, b = num(a), num(b)
    d = relative_diff(a, b)
    if d is None:
        return None
    div = d * 100.0
    return Comparison(fact, str(fact_key), source_a, a, source_b, b, div, div <= tol_pct)


def compare_absolute(fact, fact_key, source_a, a, source_b, b, tol_pts) -> Comparison | None:
    """Compare two values on their absolute difference (same unit as the values, e.g.
    percentage points). None when either side is missing."""
    a, b = num(a), num(b)
    if a is None or b is None:
        return None
    div = abs(a - b)
    return Comparison(fact, str(fact_key), source_a, a, source_b, b, div, div <= tol_pts)


# ---------- freshness ----------

def _fresh(ts: datetime | None, as_of: datetime) -> bool:
    """A captured/fetched timestamp is fresh enough to reconcile if within FRESH_DAYS."""
    if ts is None:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (as_of - ts) <= timedelta(days=FRESH_DAYS)


def _fresh_date(d: date | None, as_of: datetime) -> bool:
    return d is not None and (as_of.date() - d) <= timedelta(days=FRESH_DAYS)


def _quarter_of_date(d: date) -> str:
    """A quarter-end date -> fiscal 'YYYYQn' token (Mar->Q1, Jun->Q2, Sep->Q3, Dec->Q4)."""
    return f"{d.year}Q{(d.month + 2) // 3}"


_SCR_MONTHS = {"mar": 1, "jun": 2, "sep": 3, "dec": 4}


def _quarter_of_screener_period(label: str) -> str | None:
    """A screener period label 'Sep 2025' -> 'YYYYQn', or None if unparseable."""
    parts = str(label).strip().split()
    if len(parts) != 2:
        return None
    mon, yr = parts[0][:3].lower(), parts[1]
    if mon not in _SCR_MONTHS or not yr.isdigit():
        return None
    return f"{yr}Q{_SCR_MONTHS[mon]}"


# ---------- per-fact collectors ----------
# Each returns one of:
#   ("compared", Comparison)   two fresh sides existed and were compared
#   ("missing", None)          a side was absent — nothing to compare
#   ("stale", None)            both sides existed but at least one was too old

def price_check(session, stock_id: int, as_of: datetime):
    """bhavcopy bar close vs the yfinance snapshot's recorded last close, on the
    snapshot's last bar date."""
    yf = latest_snapshot(session, stock_id, source="yfinance")
    if yf is None:
        return "missing", None
    hs = yf.history_summary or {}
    last_close = num(hs.get("last_close"))
    last_date_s = hs.get("last_date")
    if last_close is None or not last_date_s:
        return "missing", None
    try:
        bar_date = date.fromisoformat(str(last_date_s)[:10])
    except ValueError:
        return "missing", None

    bhav_close = session.scalar(
        select(DailyPrice.close).where(
            DailyPrice.stock_id == stock_id, DailyPrice.date == bar_date
        )
    )
    if bhav_close is None:
        return "missing", None

    if not (_fresh(yf.captured_at, as_of) and _fresh_date(bar_date, as_of)):
        return "stale", None

    return "compared", compare_relative(
        "price", bar_date.isoformat(),
        "bhavcopy", bhav_close, "yfinance", last_close, PRICE_DIVERGENCE_PCT,
    )


def market_cap_check(session, stock_id: int, as_of: datetime):
    """close × sharesOutstanding (computed off the snapshot's info) vs the market cap the
    same snapshot publishes."""
    yf = latest_snapshot(session, stock_id, source="yfinance")
    if yf is None:
        return "missing", None
    info = yf.info or {}
    shares = num(info.get("sharesOutstanding"))
    price = yf.current_price if yf.current_price is not None else num(
        info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose")
    )
    published = yf.market_cap if yf.market_cap is not None else num(info.get("marketCap"))
    if shares is None or price is None or published is None:
        return "missing", None

    if not _fresh(yf.captured_at, as_of):
        return "stale", None

    key = (yf.captured_at or as_of).date().isoformat()
    return "compared", compare_relative(
        "market_cap", key,
        "computed", price * shares, "yfinance", published, MARKET_CAP_DIVERGENCE_PCT,
    )


def promoter_check(session, stock_id: int, as_of: datetime):
    """NSE shareholding-pattern promoter % vs the screener scrape's promoter %, on the
    latest fiscal quarter both sources hold."""
    scr = latest_snapshot(session, stock_id, source="screener")
    holders = (((scr.screener if scr else None) or {}).get("shareholding") or {})
    promoters = holders.get("Promoters") or {}
    scr_by_q: dict[str, float] = {}
    for label, val in promoters.items():
        q = _quarter_of_screener_period(label)
        v = num(val)
        if q and v is not None:
            scr_by_q[q] = v
    if not scr_by_q:
        return "missing", None

    nse_rows = session.scalars(
        select(ShareholdingPattern).where(ShareholdingPattern.stock_id == stock_id)
    ).all()
    nse_by_q: dict[str, ShareholdingPattern] = {}
    for row in nse_rows:
        if row.period_end is None or row.promoter_pct is None:
            continue
        nse_by_q[_quarter_of_date(row.period_end)] = row  # later rows win same quarter
    if not nse_by_q:
        return "missing", None

    common = set(scr_by_q) & set(nse_by_q)
    if not common:
        return "missing", None
    q = max(common)  # latest quarter both sources cover
    nse_row = nse_by_q[q]

    if not (_fresh(nse_row.fetched_at, as_of) and _fresh(scr.captured_at, as_of)):
        return "stale", None

    return "compared", compare_absolute(
        "promoter_pct", q,
        "nse", nse_row.promoter_pct, "screener", scr_by_q[q], PROMOTER_DIVERGENCE_PTS,
    )


CHECKS = (price_check, market_cap_check, promoter_check)


# ---------- discrepancy persistence (dedup / resolve / reopen) ----------

def record_discrepancy(session, stock_id: int, comp: Comparison, now: datetime) -> bool:
    """Upsert the one row for (stock_id, fact, fact_key). Returns True when it is a NEW
    open discrepancy — freshly inserted, or a previously-resolved one re-opening — which
    is what the wholesale-failure alert counts. A repeat of an already-open discrepancy
    updates the row (latest observed values + detected_at) and returns False."""
    existing = session.scalar(
        select(SourceDiscrepancy).where(
            SourceDiscrepancy.stock_id == stock_id,
            SourceDiscrepancy.fact == comp.fact,
            SourceDiscrepancy.fact_key == comp.fact_key,
        )
    )
    if existing is None:
        session.add(SourceDiscrepancy(
            stock_id=stock_id, fact=comp.fact, fact_key=comp.fact_key,
            source_a=comp.source_a, value_a=comp.value_a,
            source_b=comp.source_b, value_b=comp.value_b,
            divergence_pct=comp.divergence, detected_at=now, resolved_at=None,
        ))
        return True
    was_open = existing.resolved_at is None
    existing.source_a, existing.value_a = comp.source_a, comp.value_a
    existing.source_b, existing.value_b = comp.source_b, comp.value_b
    existing.divergence_pct = comp.divergence
    existing.detected_at = now
    existing.resolved_at = None
    return not was_open


def resolve_discrepancy(session, stock_id: int, fact: str, fact_key: str,
                        now: datetime) -> int:
    """Close an open discrepancy for this key because the sources now agree. Returns 1 if
    one was open (and is now resolved), else 0."""
    row = session.scalar(
        select(SourceDiscrepancy).where(
            SourceDiscrepancy.stock_id == stock_id,
            SourceDiscrepancy.fact == fact,
            SourceDiscrepancy.fact_key == fact_key,
            SourceDiscrepancy.resolved_at.is_(None),
        )
    )
    if row is None:
        return 0
    row.resolved_at = now
    return 1


# ---------- trust (rolling 90d agreement rate) ----------

def _tally_key(source: str, fact: str) -> str:
    return f"{source}|{fact}"


def _window_tally(session, now: datetime) -> tuple[dict[str, dict[str, int]], datetime | None]:
    """Sum the per-run trust tallies of prior reconcile runs inside the 90d window, from
    their stored job_run stats. Returns ({source|fact: {c, a}}, earliest run start)."""
    floor = now - timedelta(days=TRUST_WINDOW_DAYS)
    rows = session.execute(
        select(JobRun.stats, JobRun.started_at).where(
            JobRun.job_type == "reconcile",
            JobRun.status.in_(("success", "partial")),
            JobRun.started_at >= floor,
        )
    ).all()
    acc: dict[str, dict[str, int]] = defaultdict(lambda: {"c": 0, "a": 0})
    earliest: datetime | None = None
    for stats, started in rows:
        tally = (stats or {}).get("trust_tally") or {}
        if tally:
            earliest = started if earliest is None else min(earliest, started)
        for key, ca in tally.items():
            acc[key]["c"] += int(ca.get("c", 0))
            acc[key]["a"] += int(ca.get("a", 0))
    return acc, earliest


def update_trust(session, run_tally: dict[str, dict[str, int]], now: datetime) -> int:
    """Recompute the source_trust materialization: this run's tally + prior runs in the
    90d window, one upserted row per (source, fact). Returns the number of rows written."""
    prior, earliest = _window_tally(session, now)
    combined: dict[str, dict[str, int]] = defaultdict(lambda: {"c": 0, "a": 0})
    for src in (prior, run_tally):
        for key, ca in src.items():
            combined[key]["c"] += int(ca.get("c", 0))
            combined[key]["a"] += int(ca.get("a", 0))

    # The window opens at the earliest run it draws on; this run extends it to now.
    window_start = min(earliest, now) if earliest else now

    written = 0
    for key, ca in combined.items():
        source, fact = key.split("|", 1)
        row = session.scalar(
            select(SourceTrust).where(
                SourceTrust.source == source, SourceTrust.fact == fact
            )
        )
        if row is None:
            session.add(SourceTrust(
                source=source, fact=fact, agreements=ca["a"], comparisons=ca["c"],
                window_start=window_start, computed_at=now,
            ))
        else:
            row.agreements = ca["a"]
            row.comparisons = ca["c"]
            row.window_start = window_start
            row.computed_at = now
        written += 1
    return written


# ---------- the job ----------

def reconcile(limit: int = 0, verbose: bool = True) -> dict:
    """Compare every active stock's multi-source facts, record fresh discrepancies, and
    refresh the rolling trust scores. Idempotent within a run (dedup on the fact key); the
    trust window rolls forward off the job_runs history."""
    with job_run("reconcile", target="active") as (session, stats):
        now = utcnow()
        counts = {
            "stocks": 0, "compared": 0, "agreements": 0, "discrepancies": 0,
            "new_discrepancies": 0, "resolved": 0, "missing": 0, "stale": 0,
        }
        run_tally: dict[str, dict[str, int]] = defaultdict(lambda: {"c": 0, "a": 0})

        q = select(Stock).where(Stock.status.in_(("active", "new"))).order_by(Stock.symbol)
        stocks = session.scalars(q).all()
        if limit:
            stocks = stocks[:limit]

        for stock in stocks:
            counts["stocks"] += 1
            for check in CHECKS:
                kind, comp = check(session, stock.id, now)
                if kind == "stale":
                    counts["stale"] += 1
                    continue
                if kind == "missing" or comp is None:
                    counts["missing"] += 1
                    continue
                counts["compared"] += 1
                for source in (comp.source_a, comp.source_b):
                    t = run_tally[_tally_key(source, comp.fact)]
                    t["c"] += 1
                    if comp.agree:
                        t["a"] += 1
                if comp.agree:
                    counts["agreements"] += 1
                    counts["resolved"] += resolve_discrepancy(
                        session, stock.id, comp.fact, comp.fact_key, now
                    )
                else:
                    counts["discrepancies"] += 1
                    if record_discrepancy(session, stock.id, comp, now):
                        counts["new_discrepancies"] += 1
            session.commit()

        # Persist this run's tally so the next run's 90d window includes it, then roll the
        # materialized trust table.
        stats["trust_tally"] = {k: dict(v) for k, v in run_tally.items()}
        counts["trust_rows"] = update_trust(session, stats["trust_tally"], now)
        session.commit()
        stats.update(counts)

        if counts["new_discrepancies"] > NEW_DISCREPANCY_ALERT:
            from engine.notify import notify_safe
            notify_safe(
                "cross-source reconciliation: discrepancy spike",
                f"{counts['new_discrepancies']} NEW source discrepancies in one run "
                f"(threshold {NEW_DISCREPANCY_ALERT}) — a source likely went bad "
                f"wholesale. compared={counts['compared']} across {counts['stocks']} "
                f"stocks.",
                priority="high", tags="rotating_light",
            )

        if verbose:
            print(f"[reconcile] {counts}")
    return stats
