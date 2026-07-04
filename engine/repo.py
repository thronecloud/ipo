"""
Data-access layer — the single place the engine writes to Postgres.

Every scraper/fetcher/analyzer goes through these functions so that:
- snapshots are content-hashed (identical data is never stored twice),
- analyses are linked to the exact data snapshot they were based on,
- and every action is wrapped in a JobRun for the admin dashboard.
"""

import hashlib
import json
from collections import Counter
from contextlib import contextmanager

from sqlalchemy import cast, func, insert, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert

from db.base import SessionLocal
from db.models import (
    Analysis,
    AnalysisFailure,
    CompositeScore,
    CompositeScoreHistory,
    DailyPrice,
    IndexPrice,
    JobRun,
    Stock,
    StockSnapshot,
    utcnow,
)
from src.personas import PERSONAS

# Derived, not hardcoded — the council's size is defined in one place (src/personas).
TOTAL_PERSONAS = len(PERSONAS)
assert TOTAL_PERSONAS > 0, "PERSONAS is empty — the council must have members"
# Composite is coverage-weighted: mean of the per-persona 0-10 scores, ×10 → a 0-100
# scale that does NOT depend on how many personas have run (a 4-persona and a
# 10-persona stock are directly comparable). Consensus cuts at 60 / 40 on that scale.
SCORING_METHOD = "coverage_weighted_mean_x10_0_to_100"
SCORE_MIN, SCORE_MAX = 0, 10


# ---------- job observability ----------

# Above this fraction of failed items, a completed job is recorded as "partial",
# not "success" — a batch where most items errored is not a clean run.
JOB_ERROR_RATIO_PARTIAL = 0.5
# Stats keys that can serve as the item total, in preference order.
_JOB_TOTAL_KEYS = ("planned", "processed", "stocks", "audited", "total")


def _item_error_ratio(stats: dict) -> float | None:
    """Fraction of failed items in a job's stats, or None when not measurable."""
    errors = stats.get("error")
    if not isinstance(errors, int) or errors <= 0:
        return 0.0 if isinstance(errors, int) else None
    for key in _JOB_TOTAL_KEYS:
        total = stats.get(key)
        if isinstance(total, int) and total > 0:
            return errors / total
    success = stats.get("success")
    if isinstance(success, int):
        return errors / (errors + success)
    return None


@contextmanager
def job_run(job_type: str, target: str = "all"):
    """Wrap a unit of engine work in a JobRun row. Yields (session, stats_dict)."""
    session = SessionLocal()
    # target is VARCHAR(128); a large symbol list would overflow it — cap defensively.
    target = (target or "all")[:128]
    job = JobRun(job_type=job_type, target=target, status="running")
    session.add(job)
    session.commit()
    stats: dict = {}
    try:
        yield session, stats
        # Honesty gate: a run where most items failed is "partial", never a clean
        # "success" — the dashboard and alerting must see degraded batches.
        ratio = _item_error_ratio(stats)
        if ratio is not None and ratio >= JOB_ERROR_RATIO_PARTIAL:
            job.status = "partial"
            job.error = (f"{stats.get('error')} item error(s) — "
                         f"{ratio:.0%} of the batch failed")[:4000]
            from engine.notify import notify_safe
            notify_safe(f"engine job degraded: {job_type}",
                        f"target={target}\n{job.error}", tags="warning")
        else:
            job.status = "success"
        job.finished_at = utcnow()
        job.stats = stats
        session.commit()
    except Exception as e:
        session.rollback()
        job.status = "error"
        job.error = str(e)[:4000]
        job.finished_at = utcnow()
        session.commit()
        from engine.notify import notify_safe
        notify_safe(f"engine job failed: {job_type}",
                    f"target={target}\n{str(e)[:400]}", priority="high", tags="rotating_light")
        raise
    finally:
        session.close()


# ---------- hashing ----------

# Intraday market-quote fields that move every tick — they live in the extracted
# columns / daily_prices, and must NOT be in the content hash or an unchanged business
# would mint a "new" snapshot daily and needlessly re-trigger analysis staleness.
VOLATILE_INFO_KEYS = frozenset({
    "currentPrice", "regularMarketPrice", "previousClose", "regularMarketPreviousClose",
    "open", "regularMarketOpen", "dayHigh", "dayLow", "regularMarketDayHigh",
    "regularMarketDayLow", "bid", "ask", "bidSize", "askSize", "volume",
    "regularMarketVolume", "averageVolume", "averageVolume10days", "averageDailyVolume10Day",
    "marketCap", "fiftyTwoWeekLow", "fiftyTwoWeekHigh", "fiftyDayAverage",
    "twoHundredDayAverage", "postMarketPrice", "preMarketPrice", "targetMeanPrice",
    "regularMarketChange", "regularMarketChangePercent", "regularMarketTime",
})


def _hashable_payload(payload: dict) -> dict:
    """A copy of the payload with volatile market data stripped, for a STABLE hash:
    the digest reflects fundamentals (financials/statements/quarterly), not the quote."""
    p = dict(payload)
    p.pop("history_summary", None)  # last_close / avg_volume move daily
    info = p.get("info")
    if isinstance(info, dict):
        p["info"] = {k: v for k, v in info.items() if k not in VOLATILE_INFO_KEYS}
    return p


def compute_content_hash(payload: dict) -> str:
    """Stable hash of a scraped payload (order-independent, volatile-quote-independent)."""
    blob = json.dumps(_hashable_payload(payload), sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def extract_columns(info: dict) -> dict:
    """Pull the queryable convenience columns out of a yfinance info block."""
    info = info or {}
    price = info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose")
    pe = info.get("trailingPE")
    if pe is None:
        eps = _num(info.get("epsTrailingTwelveMonths"))
        p = _num(price)
        if eps and eps > 0 and p:
            pe = round(p / eps, 2)
    return {
        "current_price": _num(price),
        "market_cap": _num(info.get("marketCap")),
        "pe_ratio": _num(pe),
        "roe": _num(info.get("returnOnEquity")),
        "debt_to_equity": _num(info.get("debtToEquity")),
        "revenue_growth": _num(info.get("revenueGrowth")),
    }


# ---------- stocks ----------

def get_or_create_stock(session, symbol: str, **defaults) -> Stock:
    stock = session.scalar(select(Stock).where(Stock.symbol == symbol))
    if stock is None:
        # Conflict-safe create: a concurrent discover/amfi run inserting the same
        # symbol no longer crashes with an IntegrityError — the loser re-reads.
        stmt = (
            pg_insert(Stock)
            .values(symbol=symbol, universe=defaults.pop("universe", []), status="active")
            .on_conflict_do_nothing(index_elements=["symbol"])
            .returning(Stock.id)
        )
        new_id = session.execute(stmt).scalar()
        session.flush()
        stock = (
            session.get(Stock, new_id) if new_id is not None
            else session.scalar(select(Stock).where(Stock.symbol == symbol))
        )
    for k, v in defaults.items():
        if v is not None and getattr(stock, k, None) in (None, "", []):
            setattr(stock, k, v)
    return stock


def add_universe_tag(session, stock: Stock, tag: str):
    """Atomically append a universe tag, dedup-guarded — a single UPDATE at the DB, so
    two jobs (e.g. discover + amfi) tagging the same stock concurrently can't lose each
    other's tag the way a Python read-modify-write would."""
    if not tag:
        return
    # coalesce NULL universe -> '[]' so the append + dedup-guard work on any row.
    uni = cast(func.coalesce(cast(Stock.universe, JSONB), func.jsonb_build_array()), JSONB)
    session.execute(
        update(Stock)
        .where(Stock.id == stock.id, ~uni.contains([tag]))
        .values(universe=uni.op("||")(func.jsonb_build_array(tag)))
    )
    session.expire(stock, ["universe"])  # ORM copy is stale after the DB-side append


def universe_contains(tag: str):
    """SQL predicate for 'stock is tagged <tag>' using the JSONB @> operator (GIN-indexed)."""
    return cast(Stock.universe, JSONB).contains([tag])


def bump_fetch_failures(session, stock: Stock) -> int:
    """Atomically increment fetch_failures and return the NEW value — so the park
    decision keys off the post-increment count, not a racy Python read-modify-write."""
    n = session.execute(
        update(Stock).where(Stock.id == stock.id)
        .values(fetch_failures=func.coalesce(Stock.fetch_failures, 0) + 1)
        .returning(Stock.fetch_failures)
    ).scalar()
    session.expire(stock, ["fetch_failures"])
    return n


def reset_fetch_failures(session, stock: Stock):
    if stock.fetch_failures:
        session.execute(update(Stock).where(Stock.id == stock.id).values(fetch_failures=0))
        session.expire(stock, ["fetch_failures"])


# ---------- snapshots ----------

def latest_snapshot(session, stock_id: int, quality: str | None = None,
                    source: str | None = None) -> StockSnapshot | None:
    q = select(StockSnapshot).where(StockSnapshot.stock_id == stock_id)
    if quality:
        q = q.where(StockSnapshot.data_quality == quality)
    if source:
        q = q.where(StockSnapshot.source == source)
    # id.desc() tiebreaker → deterministic "latest" even when captured_at ties.
    return session.scalar(
        q.order_by(StockSnapshot.captured_at.desc(), StockSnapshot.id.desc()).limit(1)
    )


def add_snapshot(session, stock: Stock, payload: dict, extracted: dict, *,
                 source="yfinance", fetch_status=None, data_quality=None,
                 ipo_data=None, captured_at=None) -> tuple[StockSnapshot, bool]:
    """
    Insert a snapshot only if its content hash is new for this stock.
    Returns (snapshot, created?). `payload` holds info/financials/etc; `extracted`
    holds the convenience columns (current_price, pe_ratio, ...).
    """
    chash = compute_content_hash(payload)
    values = dict(
        stock_id=stock.id,
        captured_at=captured_at or utcnow(),
        source=source,
        fetch_status=fetch_status,
        data_quality=data_quality,
        content_hash=chash,
        info=payload.get("info"),
        financials=payload.get("financials"),
        quarterly_financials=payload.get("quarterly_financials"),
        balance_sheet=payload.get("balance_sheet"),
        cashflow=payload.get("cashflow"),
        history_summary=payload.get("history_summary"),
        screener=payload.get("screener"),
        ipo_data=ipo_data,
        **extracted,
    )
    # Conflict-safe insert on (stock_id, content_hash): concurrent writers of the same
    # snapshot never raise — one wins (RETURNING id), the other resolves to the winner.
    stmt = (
        pg_insert(StockSnapshot).values(**values)
        .on_conflict_do_nothing(index_elements=["stock_id", "content_hash"])
        .returning(StockSnapshot.id)
    )
    new_id = session.execute(stmt).scalar()
    if new_id is not None:
        session.flush()
        return session.get(StockSnapshot, new_id), True
    existing = session.scalar(
        select(StockSnapshot).where(
            StockSnapshot.stock_id == stock.id,
            StockSnapshot.content_hash == chash,
        )
    )
    return existing, False


# ---------- daily prices (OHLCV time series) ----------

def upsert_daily_prices(session, stock_id: int, rows: list[dict]) -> int:
    """Append new daily OHLCV bars for a stock; existing (stock_id, date) rows are
    left untouched (append-only). `rows` items: {date, open, high, low, close, volume}.
    Returns the count of newly-inserted bars.
    """
    if not rows:
        return 0
    # ON CONFLICT DO NOTHING is atomic + concurrency-safe: a bar another writer
    # already inserted is skipped, never an IntegrityError that aborts the batch.
    # RETURNING yields only the rows actually inserted (conflicts are skipped).
    payload = [{"stock_id": stock_id, **r} for r in rows]
    stmt = (
        pg_insert(DailyPrice).values(payload)
        .on_conflict_do_nothing(index_elements=["stock_id", "date"])
        .returning(DailyPrice.id)
    )
    return len(session.execute(stmt).fetchall())


def upsert_index_prices(session, symbol: str, rows: list[dict]) -> int:
    """Append new daily bars for a benchmark index; existing (symbol, date) rows
    are left untouched (append-only). Returns the count of newly-inserted bars."""
    if not rows:
        return 0
    payload = [{"symbol": symbol, **r} for r in rows]
    stmt = (
        pg_insert(IndexPrice).values(payload)
        .on_conflict_do_nothing(index_elements=["symbol", "date"])
        .returning(IndexPrice.id)
    )
    return len(session.execute(stmt).fetchall())


# ---------- analyses ----------

def existing_persona_hashes(session, stock_id: int) -> dict[str, set]:
    """Map persona -> set of data_hashes already analyzed (any model)."""
    rows = session.execute(
        select(Analysis.persona, Analysis.data_hash).where(Analysis.stock_id == stock_id)
    ).all()
    out: dict[str, set] = {}
    for persona, dhash in rows:
        out.setdefault(persona, set()).add(dhash)
    return out


def save_analysis(session, stock: Stock, snapshot: StockSnapshot, persona: str,
                  model: str, prompt_version: str, result: dict, meta: dict) -> Analysis:
    # LLM output is untrusted — enforce the contract before it can touch the DB.
    from engine.analysis.contract import validate_analysis_result
    validate_analysis_result(result)
    values = dict(
        stock_id=stock.id,
        persona=persona,
        model=model,
        prompt_version=prompt_version,
        snapshot_id=snapshot.id if snapshot else None,
        data_hash=snapshot.content_hash if snapshot else None,
        score=result.get("score"),
        recommendation=result.get("recommendation"),
        investment_thesis=result.get("investment_thesis"),
        key_strengths=result.get("key_strengths"),
        key_risks=result.get("key_risks"),
        red_flags=result.get("red_flags"),
        detailed_analysis=result.get("detailed_analysis"),
        metrics_evaluated=result.get("metrics_evaluated"),
        analyzed_at=utcnow(),
        duration_ms=(meta or {}).get("duration_ms"),
        total_cost_usd=(meta or {}).get("total_cost_usd"),
        usage=(meta or {}).get("usage"),
    )
    # Upsert on the natural key (stock, persona, data_hash, model): a force re-run
    # over unchanged data refreshes the verdict in place (analyzed_at moves forward
    # so recompute/reconcile pick it up) instead of minting a duplicate row.
    stmt = pg_insert(Analysis).values(**values)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_analysis_natural_key",
        set_={k: stmt.excluded[k] for k in values
              if k not in ("stock_id", "persona", "data_hash", "model")},
    ).returning(Analysis.id)
    row_id = session.execute(stmt).scalar()
    session.flush()
    row = session.get(Analysis, row_id)
    session.refresh(row)  # the ORM identity map may hold the pre-upsert state
    return row


# ---------- analysis dead-letter ----------

def record_analysis_failure(session, stock_id: int, persona: str,
                            data_hash: str, error: str) -> int:
    """Atomically bump the failure count for (stock, persona, data_hash) and
    return the NEW count. Conflict-safe: concurrent workers never lose a bump."""
    stmt = pg_insert(AnalysisFailure).values(
        stock_id=stock_id, persona=persona, data_hash=data_hash,
        failures=1, last_error=(error or "")[:4000], last_attempt_at=utcnow(),
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_analysis_failure_key",
        set_={
            "failures": AnalysisFailure.failures + 1,
            "last_error": stmt.excluded.last_error,
            "last_attempt_at": stmt.excluded.last_attempt_at,
        },
    ).returning(AnalysisFailure.failures)
    return session.execute(stmt).scalar()


def clear_analysis_failure(session, stock_id: int, persona: str, data_hash: str):
    """A successful analysis wipes the pair's dead-letter marker for that data."""
    session.query(AnalysisFailure).filter(
        AnalysisFailure.stock_id == stock_id,
        AnalysisFailure.persona == persona,
        AnalysisFailure.data_hash == data_hash,
    ).delete()


def dead_letter_pairs(session, stock_id: int, threshold: int) -> set:
    """(persona, data_hash) pairs of this stock at/over the failure threshold."""
    rows = session.execute(
        select(AnalysisFailure.persona, AnalysisFailure.data_hash)
        .where(AnalysisFailure.stock_id == stock_id,
               AnalysisFailure.failures >= threshold)
    ).all()
    return {(p, h) for p, h in rows}


# ---------- scoring ----------

def recompute_scores_for_stock(session, stock: Stock):
    """Recompute the composite score for one stock from its latest per-persona analyses.

    Deterministic + concurrency-safe: the per-persona "latest" is totally ordered
    (analyzed_at DESC, id DESC — no timestamp-tie ambiguity), and the write is an
    atomic upsert under a row lock on the parent stock, so N workers recomputing the
    same stock converge to exactly one row (uq_composite_stock) with no torn state.
    """
    # Serialize concurrent recomputes of THIS stock.
    session.execute(select(Stock.id).where(Stock.id == stock.id).with_for_update()).first()

    rows = session.scalars(
        select(Analysis)
        .where(Analysis.stock_id == stock.id)
        .order_by(Analysis.persona, Analysis.analyzed_at.desc(), Analysis.id.desc())
    ).all()
    latest: dict[str, Analysis] = {}
    for a in rows:
        if a.score is None:
            continue
        latest.setdefault(a.persona, a)  # first seen per persona = newest by ordering
    if not latest:
        return None

    # Clamp defensively (save_analysis already rejects out-of-range, but a rogue row
    # inserted by other means must never skew the composite). Sorted → stable JSON.
    persona_scores = {
        p: max(SCORE_MIN, min(SCORE_MAX, latest[p].score)) for p in sorted(latest)
    }
    composite = round(sum(persona_scores.values()) / len(persona_scores) * 10, 1)
    recs = [latest[p].recommendation for p in sorted(latest) if latest[p].recommendation]
    consensus = "BUY" if composite >= 60 else "HOLD" if composite >= 40 else "AVOID"
    # The date the view actually FORMED (max analyzed_at) — the point-in-time key.
    information_date = max(latest[p].analyzed_at for p in latest)

    # Confidence layer (LEAK#1): tier + LCB on the independent axis subspace.
    from engine.scoring.confidence import compute_confidence
    conf = compute_confidence(persona_scores, round(composite, 1))

    vals = dict(
        stock_id=stock.id,
        computed_at=utcnow(),
        composite_score=round(composite, 1),
        persona_scores=persona_scores,
        consensus_recommendation=consensus,
        recommendation_counts=dict(Counter(sorted(recs))),
        analysis_coverage=len(persona_scores),
        total_personas=TOTAL_PERSONAS,
        scoring_method=SCORING_METHOD,
        axis_scores=conf["axis_scores"],
        confidence_tier=conf["confidence_tier"],
        score_stderr_eff=conf["score_stderr_eff"],
        lcb=conf["lcb"],
        factor_version=conf["factor_version"],
    )
    stmt = pg_insert(CompositeScore).values(**vals)
    stmt = stmt.on_conflict_do_update(
        index_elements=["stock_id"],
        set_={k: stmt.excluded[k] for k in vals if k != "stock_id"},
    )
    session.execute(stmt)

    # Append-only point-in-time history — one row per (stock, day), idempotent within
    # the day (a re-recompute refreshes, never duplicates). This is what a backtest
    # reads; deferring it loses cohorts permanently.
    hist = dict(
        stock_id=stock.id,
        as_of_date=utcnow().date(),
        information_date=information_date,
        composite_score=vals["composite_score"],
        persona_scores=persona_scores,
        axis_scores=conf["axis_scores"],
        confidence_tier=conf["confidence_tier"],
        score_stderr_eff=conf["score_stderr_eff"],
        lcb=conf["lcb"],
        factor_version=conf["factor_version"],
        consensus_recommendation=consensus,
        analysis_coverage=len(persona_scores),
    )
    hstmt = pg_insert(CompositeScoreHistory).values(**hist)
    hstmt = hstmt.on_conflict_do_update(
        index_elements=["stock_id", "as_of_date"],
        set_={k: hstmt.excluded[k] for k in hist if k not in ("stock_id", "as_of_date")},
    )
    session.execute(hstmt)
    session.flush()
    return session.scalar(
        select(CompositeScore).where(CompositeScore.stock_id == stock.id)
    )


def reconcile_scores(session, limit: int = 0) -> int:
    """Recompute composites for stocks whose latest analysis is newer than their
    composite (or which have analyses but no composite — orphans left by an
    interrupted analyze batch). Returns the number rescored. Idempotent: converges
    to zero work once every composite is current."""
    latest_analysis = (
        select(Analysis.stock_id, func.max(Analysis.analyzed_at).label("a"))
        .group_by(Analysis.stock_id)
        .subquery()
    )
    cs = select(
        CompositeScore.stock_id,
        CompositeScore.computed_at,
        CompositeScore.scoring_method,
    ).subquery()
    q = (
        select(Stock)
        .join(latest_analysis, latest_analysis.c.stock_id == Stock.id)
        .outerjoin(cs, cs.c.stock_id == Stock.id)
        .where(
            (cs.c.stock_id.is_(None))                      # orphan: no composite
            | (latest_analysis.c.a > cs.c.computed_at)     # stale: newer analysis
            | (cs.c.scoring_method != SCORING_METHOD)      # scored under an old formula
        )
        .order_by(Stock.id)
    )
    stocks = session.scalars(q).all()
    if limit:
        stocks = stocks[:limit]
    n = 0
    for st in stocks:
        if recompute_scores_for_stock(session, st) is not None:
            n += 1
        session.commit()
    return n
