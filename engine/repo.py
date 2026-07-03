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

from sqlalchemy import cast, select
from sqlalchemy.dialects.postgresql import JSONB

from db.base import SessionLocal
from db.models import (
    Analysis,
    CompositeScore,
    JobRun,
    Stock,
    StockSnapshot,
    utcnow,
)

TOTAL_PERSONAS = 10
SCORING_METHOD = "sum_of_persona_scores_each_0_to_10"


# ---------- job observability ----------

@contextmanager
def job_run(job_type: str, target: str = "all"):
    """Wrap a unit of engine work in a JobRun row. Yields (session, stats_dict)."""
    session = SessionLocal()
    job = JobRun(job_type=job_type, target=target, status="running")
    session.add(job)
    session.commit()
    stats: dict = {}
    try:
        yield session, stats
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
        try:
            from engine.notify import notify
            notify(f"engine job failed: {job_type}",
                   f"target={target}\n{str(e)[:400]}", priority="high", tags="rotating_light")
        except Exception:
            pass
        raise
    finally:
        session.close()


# ---------- hashing ----------

def compute_content_hash(payload: dict) -> str:
    """Stable hash of a scraped payload (order-independent)."""
    blob = json.dumps(payload, sort_keys=True, default=str)
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
        stock = Stock(symbol=symbol, universe=defaults.pop("universe", []), status="active")
        session.add(stock)
        session.flush()
    for k, v in defaults.items():
        if v is not None and getattr(stock, k, None) in (None, "", []):
            setattr(stock, k, v)
    return stock


def add_universe_tag(stock: Stock, tag: str):
    if tag and tag not in (stock.universe or []):
        stock.universe = (stock.universe or []) + [tag]


def universe_contains(tag: str):
    """SQL predicate for 'stock is tagged <tag>' using the JSONB @> operator (GIN-indexed)."""
    return cast(Stock.universe, JSONB).contains([tag])


# ---------- snapshots ----------

def latest_snapshot(session, stock_id: int, quality: str | None = None,
                    source: str | None = None) -> StockSnapshot | None:
    q = select(StockSnapshot).where(StockSnapshot.stock_id == stock_id)
    if quality:
        q = q.where(StockSnapshot.data_quality == quality)
    if source:
        q = q.where(StockSnapshot.source == source)
    return session.scalar(q.order_by(StockSnapshot.captured_at.desc()).limit(1))


def add_snapshot(session, stock: Stock, payload: dict, extracted: dict, *,
                 source="yfinance", fetch_status=None, data_quality=None,
                 ipo_data=None, captured_at=None) -> tuple[StockSnapshot, bool]:
    """
    Insert a snapshot only if its content hash is new for this stock.
    Returns (snapshot, created?). `payload` holds info/financials/etc; `extracted`
    holds the convenience columns (current_price, pe_ratio, ...).
    """
    chash = compute_content_hash(payload)
    existing = session.scalar(
        select(StockSnapshot).where(
            StockSnapshot.stock_id == stock.id,
            StockSnapshot.content_hash == chash,
        )
    )
    if existing:
        return existing, False

    snap = StockSnapshot(
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
    session.add(snap)
    session.flush()
    return snap, True


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
    row = Analysis(
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
        duration_ms=(meta or {}).get("duration_ms"),
        total_cost_usd=(meta or {}).get("total_cost_usd"),
        usage=(meta or {}).get("usage"),
    )
    session.add(row)
    session.flush()
    return row


# ---------- scoring ----------

def recompute_scores_for_stock(session, stock: Stock):
    """Recompute the composite score for one stock from its latest per-persona analyses."""
    analyses = session.scalars(
        select(Analysis).where(Analysis.stock_id == stock.id)
    ).all()
    latest: dict[str, Analysis] = {}
    for a in analyses:
        if a.score is None:
            continue
        cur = latest.get(a.persona)
        if cur is None or a.analyzed_at > cur.analyzed_at:
            latest[a.persona] = a
    if not latest:
        return None

    persona_scores = {p: a.score for p, a in latest.items()}
    composite = sum(persona_scores.values())
    recs = [a.recommendation for a in latest.values() if a.recommendation]
    consensus = "BUY" if composite >= 60 else "HOLD" if composite >= 40 else "AVOID"

    # Replace any prior score row for this stock.
    session.query(CompositeScore).filter(CompositeScore.stock_id == stock.id).delete()
    row = CompositeScore(
        stock_id=stock.id,
        composite_score=round(composite, 1),
        persona_scores=persona_scores,
        consensus_recommendation=consensus,
        recommendation_counts=dict(Counter(recs)),
        analysis_coverage=len(persona_scores),
        total_personas=TOTAL_PERSONAS,
        scoring_method=SCORING_METHOD,
    )
    session.add(row)
    session.flush()
    return row
