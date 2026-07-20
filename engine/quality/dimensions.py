"""
The data-quality scoring model.

`score_stock(session, stock)` returns a scorecard across seven weighted dimensions.
Each dimension yields (score 0-1, status: pass|partial|fail|na, detail). A stock with
no usable data at *any* source ("source-dark", typical of BSE-only SME numeric tickers)
has its source-dependent dimensions marked `na` and is judged only on identity — so a
genuine source limit never masquerades as an F we could fix.

Correctness NEVER mutates data: mismatches become flags for human review (decision #3).
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from db.models import DailyPrice
from engine.quality.metrics import (
    cr_to_absolute,
    expected_latest_quarter,
    gaps,
    num,
    pct_to_fraction,
    quarter_ordinal,
    relative_diff,
    scr_quarters,
    yf_quarters,
)
from engine.repo import latest_snapshot

WEIGHTS = {
    "identity": 0.15,
    "yfinance": 0.15,
    "screener": 0.20,
    "prices": 0.10,
    "quarters": 0.15,
    "freshness": 0.10,
    "correctness": 0.15,
}

_QUALITY_SCORE = {"full": 1.0, "partial": 0.6, "limited": 0.3, "minimal": 0.1}
QUARTERS_TARGET = 8  # two years of quarters = "complete"

# Cross-source agreement tolerances (relative diff). Units are normalized first.
_TOLERANCE = {"price": 0.05, "market_cap": 0.10, "pe": 0.10, "roe": 0.15}


def _dim(score, status, detail):
    return {"score": score, "status": status, "detail": detail}


def _now():
    return datetime.now(timezone.utc)


# ---------- source viability ----------

def _viability(yf, scr_snap):
    """A source is 'viable' when it actually holds real data for this stock."""
    yf_viable = yf is not None and yf.data_quality in ("full", "partial")
    data = (scr_snap.screener if scr_snap else None) or {}
    scr_viable = bool(data.get("profit_loss") or data.get("ratios"))
    return yf_viable, scr_viable


# ---------- price coverage ----------

def _price_coverage(session, stock_id):
    row = session.execute(
        select(func.count(DailyPrice.id), func.max(DailyPrice.date)).where(
            DailyPrice.stock_id == stock_id
        )
    ).one()
    return row[0] or 0, row[1]


# A one-day move outside this band is not a market move. Indian equities carry 2/5/10/20%
# circuit limits, so even a limit-down day cannot reach -35%; a series that drops further
# in a single session is almost always a corporate action (split/bonus/demerger) the
# provider failed to back-adjust, leaving a return that never happened. The band is
# asymmetric because large upside gaps are far more often genuine — thin SME counters and
# post-listing re-openings really do print +50% — while a large downside gap is the
# signature of an unadjusted split (1:2 = -50%, 1:10 = -90%).
CLIFF_BAND = (-0.35, 0.55)

# Only cliffs inside this trailing window are reported. A cliff matters when a consumer
# can read across it: the backtest anchors a cohort on its information_date and measures
# forward returns out to 126 trading days (~6 calendar months), and the analysis prompt
# reads a trailing 52-week window of the same series. The 6-month leg runs forward
# (toward newer bars), so the OLDEST bar any consumer reads across is (oldest live
# information_date - 1y). Three years therefore leaves room for two full years
# (3y - 1y) of accumulated cohort history beyond today's oldest analysis before the
# window could ever clip a relevant cliff.
#
# Without the floor the flag is noise: two-thirds of production's cliffs are pre-2010
# Yahoo adjusted-close artifacts — including a single-day cluster of 159 bars in 2005 —
# that no study or prompt can touch, and they bury the handful that are actionable.
CLIFF_LOOKBACK = timedelta(days=3 * 365)


def _price_cliffs(session, stock_id, floor):
    """Adjacent-day moves outside CLIFF_BAND on or after `floor`, as
    (count, latest_date, latest_return), or None when the series is clean.

    Reports the most RECENT cliff, not the largest: within the window the largest move
    is still usually an artifact, while the recent unadjusted split is what corrupts a
    live study. Ranking by magnitude would let the former mask the latter.

    Bars with a non-positive previous close are skipped: those same artifacts leave
    negative closes in old history, and a ratio against a negative base is meaningless —
    it would report a cliff that is really just a bad base.
    """
    prev = func.lag(DailyPrice.close).over(
        partition_by=DailyPrice.stock_id, order_by=DailyPrice.date
    )
    series = (
        select(DailyPrice.date.label("date"), DailyPrice.close.label("close"),
               prev.label("prev"))
        .where(DailyPrice.stock_id == stock_id)
        .subquery()
    )
    ret = (series.c.close / series.c.prev - 1).label("ret")
    cliffs = (
        select(series.c.date, ret)
        # The lag runs over the whole series, so a cliff on the first in-window bar
        # still compares against its true predecessor; only the report is windowed.
        .where(series.c.date >= floor, series.c.prev > 0, series.c.close.isnot(None),
               (ret < CLIFF_BAND[0]) | (ret > CLIFF_BAND[1]))
        .subquery()
    )
    row = session.execute(
        select(func.count(), func.max(cliffs.c.date)).select_from(cliffs)
    ).one()
    if not row[0]:
        return None
    latest_ret = session.execute(
        select(cliffs.c.ret).where(cliffs.c.date == row[1]).limit(1)
    ).scalar()
    return row[0], row[1], latest_ret


# ---------- per-dimension scorers ----------

# Identity fields carry provenance: a value gapfill imputed must not earn the same
# credit as a measured one, or filling a hole raises the grade and the audit ends up
# validating its own guesses. Measured (yfinance/amfi) scores full; imputed
# (screener breadcrumb) and pre-provenance ('unknown') score half. An absent marker
# is a direct pre-provenance write, not evidence of imputation — it keeps full credit.
_IMPUTED_CREDIT = {"screener": 0.5, "unknown": 0.5}


def _identity_fields(stock):
    """Identity field -> (value, source). `source` is None for fields with no
    provenance column (cap_category and the ipo-only fields)."""
    fields = {
        "isin": (stock.isin, stock.isin_source),
        "sector": (stock.sector, stock.sector_source),
        "industry": (stock.industry, stock.industry_source),
        "cap_category": (stock.cap_category, None),
    }
    if any(str(t).startswith("ipo_") for t in (stock.universe or [])):
        fields["listing_date"] = (stock.listing_date, None)
        fields["issue_price"] = (stock.issue_price, None)
    return fields


def _field_credit(value, source):
    if value in (None, "", []):
        return 0.0
    return _IMPUTED_CREDIT.get(source, 1.0)


def identity_score(stock) -> float:
    fields = _identity_fields(stock)
    return sum(_field_credit(v, s) for v, s in fields.values()) / len(fields)


def _identity(stock, missing):
    fields = _identity_fields(stock)
    present = [k for k, (v, _) in fields.items() if v not in (None, "", [])]
    imputed = [k for k, (v, s) in fields.items()
               if v not in (None, "", []) and s in _IMPUTED_CREDIT]
    for k, (v, _) in fields.items():
        if v in (None, "", []):
            missing.append(f"identity:{k}")
    score = identity_score(stock)
    status = "pass" if score == 1 else "partial" if score > 0 else "fail"
    # A present-but-imputed field lowers the score without being "missing" (it is
    # not fillable — gapfill routes on `missing`). Name it so a "partial" grade on a
    # fully-populated stock is explainable instead of contradictory.
    detail = f"{len(present)}/{len(fields)} identity fields"
    if imputed:
        detail += f", {len(imputed)} imputed ({', '.join(imputed)})"
    return _dim(round(score, 3), status, detail)


def _yfinance(yf, flags, missing):
    if yf is None:
        missing.append("yfinance")
        return _dim(0.0, "fail", "no yfinance snapshot")
    q = yf.data_quality or "minimal"
    score = _QUALITY_SCORE.get(q, 0.0)
    if q in ("minimal", "limited"):
        flags.append({"type": "minimal_yf", "severity": "warn", "detail": f"quality={q}"})
        missing.append("yfinance_refresh")
    annual = bool(yf.financials)
    quarterly = bool(yf.quarterly_financials)
    status = "pass" if score >= 1 else "partial" if score >= 0.3 else "fail"
    return _dim(round(score, 3), status,
                f"quality={q} annual={'y' if annual else 'n'} quarterly={'y' if quarterly else 'n'}")


def _screener(scr_snap, flags, missing):
    data = (scr_snap.screener if scr_snap else None) or {}
    if not data:
        missing.append("screener")
        flags.append({"type": "screener_missing", "severity": "warn", "detail": "no screener snapshot"})
        return _dim(0.0, "fail", "no screener snapshot")
    sections = ["profit_loss", "quarterly_results", "ratios", "shareholding"]
    present = [s for s in sections if data.get(s)]
    if len(present) < len(sections):
        missing.append("screener")  # re-enrich to complete sections
    score = len(present) / len(sections)
    status = "pass" if score >= 1 else "partial" if score > 0 else "fail"
    return _dim(round(score, 3), status, f"{len(present)}/4 screener sections")


def _prices(n, last_bar, cliff, yf_viable, flags, missing, as_of):
    if not yf_viable:
        return _dim(None, "na", "no yfinance price history available")
    if n == 0:
        missing.append("prices")
        return _dim(0.0, "fail", "no daily bars")
    if cliff:
        # A flag, not a score cut: the series is fully covered and current, so the
        # coverage score is honest. What is wrong is a *value*, and per this module's
        # contract correctness issues surface as flags for human review. It is also
        # not fillable — re-fetching returns the same unadjusted series from Yahoo.
        n_cliffs, cliff_date, cliff_ret = cliff
        flags.append({
            "type": "price_cliff", "severity": "warn",
            "detail": f"{n_cliffs} impossible one-day move(s); latest "
                      f"{cliff_ret * 100:+.1f}% on {cliff_date} — likely a corporate "
                      f"action the provider never back-adjusted",
        })
    age = (as_of.date() - last_bar).days if last_bar else 9999
    if age <= 7:
        return _dim(1.0, "pass", f"{n} bars, current")
    flags.append({"type": "stale_prices", "severity": "info", "detail": f"last bar {age}d old"})
    missing.append("stale_prices")  # fillable: backfill_prices extends the series
    return _dim(0.5, "partial", f"{n} bars, {age}d stale")


def _quarters(yf, scr_snap, yf_viable, scr_viable, flags, missing, as_of):
    if not (yf_viable or scr_viable):
        return _dim(None, "na", "no source with quarterly data")
    yq = yf_quarters(yf.quarterly_financials if yf else None)
    sq = scr_quarters(((scr_snap.screener or {}) if scr_snap else {}).get("quarterly_results"))
    best = yq if len(yq) >= len(sq) else sq
    n, g = len(best), gaps(best)
    if n < QUARTERS_TARGET:
        missing.append("quarters")
    if g > 0:
        flags.append({"type": "missing_quarters", "severity": "warn", "detail": f"{g} gap(s) in {n}q"})

    # Currency: is the newest quarter we hold as recent as it should be by now?
    all_q = sorted(set(yq) | set(sq))
    latest_held = all_q[-1] if all_q else None
    expected = expected_latest_quarter(as_of.date())
    stale = bool(
        expected and latest_held
        and quarter_ordinal(latest_held) < quarter_ordinal(expected)
    )
    if stale:
        behind = quarter_ordinal(expected) - quarter_ordinal(latest_held)
        flags.append({"type": "stale_quarter", "severity": "warn",
                      "detail": f"latest held {latest_held}, expected {expected} ({behind}q behind)"})
        missing.append("latest_quarter")

    score = min(1.0, n / QUARTERS_TARGET) * (1.0 if g == 0 else 0.7) * (0.7 if stale else 1.0)
    passing = n >= QUARTERS_TARGET and g == 0 and not stale
    status = "pass" if passing else "partial" if n >= 4 else "fail"
    detail = f"{n}q latest {latest_held or '—'}" + (f" STALE→{expected}" if stale else "")
    return _dim(round(score, 3), status, detail)


def _correctness(yf, scr_snap, yf_viable, scr_viable, flags):
    if not (yf_viable and scr_viable):
        return _dim(None, "na", "need both sources to cross-check")
    r = ((scr_snap.screener or {}) if scr_snap else {}).get("ratios") or {}
    checks = [
        ("price", "price_mismatch", yf.current_price, num(r.get("Current Price")), _TOLERANCE["price"]),
        ("market_cap", "mcap_mismatch", yf.market_cap, cr_to_absolute(r.get("Market Cap")), _TOLERANCE["market_cap"]),
        ("pe", "pe_mismatch", yf.pe_ratio, num(r.get("Stock P/E")), _TOLERANCE["pe"]),
        ("roe", "roe_mismatch", yf.roe, pct_to_fraction(r.get("ROE")), _TOLERANCE["roe"]),
    ]
    agree = comparable = 0
    for name, flag_type, a, b, tol in checks:
        d = relative_diff(a, b)
        if d is None:
            continue
        comparable += 1
        if d <= tol:
            agree += 1
        else:
            flags.append({
                "type": flag_type, "severity": "warn",
                "detail": f"yf={a} vs screener={b} ({d*100:.0f}% apart)",
            })
    if comparable == 0:
        return _dim(None, "na", "no comparable metrics")
    score = agree / comparable
    status = "pass" if score == 1 else "partial" if score >= 0.5 else "fail"
    return _dim(round(score, 3), status, f"{agree}/{comparable} metrics agree")


def _freshness(yf, scr_snap, flags, as_of):
    times = [s.captured_at for s in (yf, scr_snap) if s and s.captured_at]
    if not times:
        return _dim(None, "na", "no snapshot")
    age = (as_of - max(times)).days
    if age < 8:
        return _dim(1.0, "pass", f"{age}d old")
    flags.append({"type": "stale", "severity": "warn", "detail": f"latest snapshot {age}d old"})
    return _dim(0.5 if age < 31 else 0.1, "partial" if age < 31 else "fail", f"{age}d old")


def _aggregate(dims):
    num_ = den = 0.0
    for name, d in dims.items():
        if d["status"] == "na" or d["score"] is None:
            continue
        w = WEIGHTS[name]
        num_ += w * d["score"]
        den += w
    overall = round(100 * num_ / den, 1) if den else None
    grade = None
    if overall is not None:
        grade = ("A" if overall >= 90 else "B" if overall >= 75
                 else "C" if overall >= 55 else "D" if overall >= 35 else "F")
    return overall, grade


# ---------- public entry point ----------

def score_stock(session, stock, as_of=None) -> dict:
    # `as_of` is the single time reference for the whole scorecard — passed in (once
    # per audit run) so re-scoring unchanged data yields an identical grade, instead
    # of drifting with wall-clock `_now()`.
    as_of = as_of or _now()
    yf = latest_snapshot(session, stock.id, source="yfinance")
    scr = latest_snapshot(session, stock.id, source="screener")
    n_prices, last_bar = _price_coverage(session, stock.id)
    cliff = (_price_cliffs(session, stock.id, as_of.date() - CLIFF_LOOKBACK)
             if n_prices else None)
    yf_viable, scr_viable = _viability(yf, scr)
    source_dark = not yf_viable and not scr_viable

    flags: list = []
    missing: list = []
    dims: dict = {"identity": _identity(stock, missing)}

    if source_dark:
        for d in ("yfinance", "screener", "prices", "quarters", "correctness"):
            dims[d] = _dim(None, "na", "source-unavailable")
        flags.append({"type": "source_unavailable", "severity": "info",
                      "detail": "no usable data at yfinance or screener"})
    else:
        dims["yfinance"] = _yfinance(yf, flags, missing)
        dims["screener"] = _screener(scr, flags, missing)
        dims["prices"] = _prices(n_prices, last_bar, cliff, yf_viable, flags, missing, as_of)
        dims["quarters"] = _quarters(yf, scr, yf_viable, scr_viable, flags, missing, as_of)
        dims["correctness"] = _correctness(yf, scr, yf_viable, scr_viable, flags)

    dims["freshness"] = _freshness(yf, scr, flags, as_of)

    overall, grade = _aggregate(dims)
    return {
        "overall_score": overall,
        "grade": grade,
        "dimensions": dims,
        "flags": flags,
        "missing": sorted(set(missing)),
    }
