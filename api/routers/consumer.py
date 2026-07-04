"""Consumer 'Research Desk' endpoints — meta, stock list, stock detail."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from engine.backtest.study import DEFAULT_BENCHMARK, run_event_study

from api.deps import (
    PERSONA_ORDER,
    TOTAL_PERSONAS,
    get_db,
    ipo_return_pct,
    persona_meta,
    personas_payload,
    to_cr,
)
from api.schemas import (
    Candle,
    CandleSeries,
    Composite,
    CouncilMember,
    Fundamentals,
    Identity,
    Meta,
    Quote,
    StockDetail,
    StockList,
    StockListItem,
    ConvictionPoint,
    PersonaVerdict,
)
from db.models import Analysis, CompositeScore, DailyPrice, Stock, StockSnapshot
from engine.repo import universe_contains

router = APIRouter(prefix="/api", tags=["consumer"])


# ---- reusable subqueries -------------------------------------------------

def _latest_yf_snapshot_sq():
    """DISTINCT ON (stock_id) latest yfinance snapshot — the quote source."""
    return (
        select(
            StockSnapshot.stock_id.label("stock_id"),
            StockSnapshot.current_price,
            StockSnapshot.market_cap,
            StockSnapshot.pe_ratio,
            StockSnapshot.roe,
            StockSnapshot.debt_to_equity,
            StockSnapshot.revenue_growth,
            StockSnapshot.data_quality,
            StockSnapshot.captured_at,
        )
        .where(StockSnapshot.source == "yfinance")
        .order_by(StockSnapshot.stock_id, StockSnapshot.captured_at.desc(), StockSnapshot.id.desc())
        .distinct(StockSnapshot.stock_id)
        .subquery()
    )


def _latest_composite_sq():
    """DISTINCT ON (stock_id) latest composite score row."""
    return (
        select(
            CompositeScore.stock_id.label("stock_id"),
            CompositeScore.composite_score,
            CompositeScore.consensus_recommendation,
            CompositeScore.analysis_coverage,
            CompositeScore.persona_scores,
            CompositeScore.computed_at,
        )
        .order_by(CompositeScore.stock_id, CompositeScore.computed_at.desc(), CompositeScore.id.desc())
        .distinct(CompositeScore.stock_id)
        .subquery()
    )


# ---- endpoints -----------------------------------------------------------

@router.get("/meta", response_model=Meta)
def get_meta(db: Session = Depends(get_db)):
    stocks_total = db.scalar(select(func.count(Stock.id))) or 0
    analyzed = db.scalar(select(func.count(distinct(CompositeScore.stock_id)))) or 0

    universes: dict[str, int] = {}
    for (uni,) in db.execute(select(Stock.universe)).all():
        for tag in uni or []:
            universes[tag] = universes.get(tag, 0) + 1
    universes = dict(sorted(universes.items(), key=lambda kv: -kv[1]))

    sectors = [
        s for (s,) in db.execute(
            select(distinct(Stock.sector)).where(Stock.sector.isnot(None)).order_by(Stock.sector)
        ).all()
    ]

    from db.models import JobRun
    last_job_at = db.scalar(select(func.max(JobRun.started_at)))

    return Meta(
        stocks_total=stocks_total,
        analyzed=analyzed,
        universes=universes,
        sectors=sectors,
        last_job_at=last_job_at,
        personas=personas_payload(),
    )


@router.get("/stocks", response_model=StockList)
def list_stocks(
    db: Session = Depends(get_db),
    universe: str | None = None,
    sector: str | None = None,
    cap: str | None = None,
    consensus: str | None = None,
    q: str | None = None,
    min_score: float | None = None,
    max_score: float | None = None,
    analyzed_only: bool = False,
    sort: str = "composite_score",
    order: str = "desc",
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    snap = _latest_yf_snapshot_sq()
    cs = _latest_composite_sq()

    base = (
        select(Stock, cs, snap)
        .outerjoin(cs, cs.c.stock_id == Stock.id)
        .outerjoin(snap, snap.c.stock_id == Stock.id)
    )

    # --- filters (all pushed to SQL) ---
    if universe:
        base = base.where(universe_contains(universe))
    if sector:
        base = base.where(Stock.sector == sector)
    if cap:
        base = base.where(Stock.cap_category == cap)
    if consensus:
        base = base.where(cs.c.consensus_recommendation == consensus.upper())
    if q:
        like = f"%{q}%"
        base = base.where(Stock.symbol.ilike(like) | Stock.company_name.ilike(like))
    if analyzed_only:
        base = base.where(cs.c.composite_score.isnot(None))
    if min_score is not None:
        base = base.where(cs.c.composite_score >= min_score)
    if max_score is not None:
        base = base.where(cs.c.composite_score <= max_score)

    # --- sorting ---
    sort_map = {
        "symbol": Stock.symbol,
        "market_cap_cr": snap.c.market_cap,
        "pe_ratio": snap.c.pe_ratio,
        "revenue_growth": snap.c.revenue_growth,
        "composite_score": cs.c.composite_score,
    }
    if sort in sort_map:
        col = sort_map[sort]
    elif sort in PERSONA_ORDER:
        # order by that persona's score inside the persona_scores JSON map
        col = cs.c.persona_scores[sort].as_float()
    else:
        col = cs.c.composite_score

    descending = order.lower() != "asc"
    ordering = col.desc() if descending else col.asc()
    # NULLs last regardless of direction so unanalyzed stocks sink to the bottom
    ordering = ordering.nulls_last()

    base = base.order_by(ordering, Stock.symbol.asc())

    # --- total (count over the filtered set) ---
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0

    rows = db.execute(base.offset((page - 1) * page_size).limit(page_size)).all()

    stock_ids = [r[0].id for r in rows]
    per_persona_by_stock = _per_persona_for_stocks(db, stock_ids)

    items: list[StockListItem] = []
    for row in rows:
        stock: Stock = row[0]
        # composite subquery columns (offset after Stock)
        comp_score = row._mapping.get("composite_score")
        consensus_rec = row._mapping.get("consensus_recommendation")
        coverage = row._mapping.get("analysis_coverage")
        items.append(
            StockListItem(
                symbol=stock.symbol,
                company_name=stock.company_name,
                sector=stock.sector,
                cap_category=stock.cap_category,
                universe=stock.universe or [],
                status=stock.status,
                composite_score=comp_score,
                consensus_recommendation=consensus_rec,
                analysis_coverage=coverage,
                composite_updated_at=row._mapping.get("computed_at"),
                per_persona=per_persona_by_stock.get(stock.id, {}),
                current_price=row._mapping.get("current_price"),
                market_cap_cr=to_cr(row._mapping.get("market_cap")),
                pe_ratio=row._mapping.get("pe_ratio"),
                roe=row._mapping.get("roe"),
                debt_to_equity=row._mapping.get("debt_to_equity"),
                revenue_growth=row._mapping.get("revenue_growth"),
                ipo_return_pct=ipo_return_pct(stock.issue_price, row._mapping.get("current_price")),
                data_quality=row._mapping.get("data_quality"),
            )
        )

    return StockList(total=total, page=page, page_size=page_size, items=items)


def _per_persona_for_stocks(db: Session, stock_ids: list[int]) -> dict[int, dict[str, PersonaVerdict]]:
    """Latest analysis per (stock, persona) for the given stock ids."""
    if not stock_ids:
        return {}
    sq = (
        select(
            Analysis.stock_id,
            Analysis.persona,
            Analysis.score,
            Analysis.recommendation,
        )
        .where(Analysis.stock_id.in_(stock_ids))
        .order_by(Analysis.stock_id, Analysis.persona, Analysis.analyzed_at.desc(), Analysis.id.desc())
        .distinct(Analysis.stock_id, Analysis.persona)
    )
    out: dict[int, dict[str, PersonaVerdict]] = {}
    for stock_id, persona, score, rec in db.execute(sq).all():
        out.setdefault(stock_id, {})[persona] = PersonaVerdict(score=score, recommendation=rec)
    return out


@router.get("/stocks/{symbol}", response_model=StockDetail)
def stock_detail(symbol: str, db: Session = Depends(get_db)):
    stock = db.scalar(select(Stock).where(Stock.symbol == symbol.upper()))
    if stock is None:
        raise HTTPException(status_code=404, detail=f"Unknown symbol: {symbol}")

    # --- quote: latest yfinance snapshot ---
    yf = db.scalar(
        select(StockSnapshot)
        .where(StockSnapshot.stock_id == stock.id, StockSnapshot.source == "yfinance")
        .order_by(StockSnapshot.captured_at.desc(), StockSnapshot.id.desc())
        .limit(1)
    )
    quote = Quote(
        current_price=yf.current_price if yf else None,
        market_cap_cr=to_cr(yf.market_cap) if yf else None,
        pe_ratio=yf.pe_ratio if yf else None,
        roe=yf.roe if yf else None,
        debt_to_equity=yf.debt_to_equity if yf else None,
        revenue_growth=yf.revenue_growth if yf else None,
        ipo_return_pct=ipo_return_pct(stock.issue_price, yf.current_price if yf else None),
        data_quality=yf.data_quality if yf else None,
        as_of=yf.captured_at if yf else None,
    )

    # --- composite: latest ---
    cs = db.scalar(
        select(CompositeScore)
        .where(CompositeScore.stock_id == stock.id)
        .order_by(CompositeScore.computed_at.desc(), CompositeScore.id.desc())
        .limit(1)
    )
    composite = Composite(
        composite_score=cs.composite_score if cs else None,
        consensus_recommendation=cs.consensus_recommendation if cs else None,
        recommendation_counts=(cs.recommendation_counts if cs else {}) or {},
        analysis_coverage=cs.analysis_coverage if cs else None,
        total_personas=(cs.total_personas if cs else None) or TOTAL_PERSONAS,
        updated_at=cs.computed_at if cs else None,
    )

    # --- council: latest analysis per persona, fixed order ---
    analyses = db.scalars(
        select(Analysis)
        .where(Analysis.stock_id == stock.id)
        .order_by(Analysis.analyzed_at.desc(), Analysis.id.desc())
    ).all()
    latest: dict[str, Analysis] = {}
    for a in analyses:
        latest.setdefault(a.persona, a)  # first seen = latest (desc order)

    council: list[CouncilMember] = []
    for slug in PERSONA_ORDER:
        meta = persona_meta(slug)
        a = latest.get(slug)
        council.append(
            CouncilMember(
                persona=slug,
                display_name=meta["display_name"],
                nationality=meta["nationality"],
                score=a.score if a else None,
                recommendation=a.recommendation if a else None,
                investment_thesis=a.investment_thesis if a else None,
                key_strengths=(a.key_strengths if a else []) or [],
                key_risks=(a.key_risks if a else []) or [],
                red_flags=(a.red_flags if a else []) or [],
                detailed_analysis=a.detailed_analysis if a else None,
                metrics_evaluated=a.metrics_evaluated if a else None,
                model=a.model if a else None,
                analyzed_at=a.analyzed_at if a else None,
            )
        )

    fundamentals = _fundamentals(db, stock)

    # --- conviction history ---
    history = db.scalars(
        select(CompositeScore)
        .where(CompositeScore.stock_id == stock.id)
        .order_by(CompositeScore.computed_at.asc())
    ).all()
    conviction_history = [
        ConvictionPoint(
            computed_at=h.computed_at,
            composite_score=h.composite_score,
            consensus_recommendation=h.consensus_recommendation,
        )
        for h in history
    ]

    identity = Identity(
        symbol=stock.symbol,
        company_name=stock.company_name,
        isin=stock.isin,
        sector=stock.sector,
        industry=stock.industry,
        exchange=stock.exchange,
        cap_category=stock.cap_category,
        universe=stock.universe or [],
        listing_date=stock.listing_date,
        status=stock.status,
    )

    return StockDetail(
        identity=identity,
        quote=quote,
        composite=composite,
        council=council,
        fundamentals=fundamentals,
        conviction_history=conviction_history,
    )


_RANGE_DAYS = {"1m": 31, "3m": 92, "6m": 183, "1y": 365, "3y": 1095, "5y": 1826}


@router.get("/stocks/{symbol}/candles", response_model=CandleSeries)
def stock_candles(
    symbol: str,
    range: str = Query("1y", pattern="^(1m|3m|6m|1y|3y|5y|max)$"),
    db: Session = Depends(get_db),
):
    """Daily OHLCV series for the price chart. `range` windows off the latest
    available bar (not wall-clock today) so a window is never empty on stale data."""
    stock = db.scalar(select(Stock).where(Stock.symbol == symbol.upper()))
    if stock is None:
        raise HTTPException(status_code=404, detail=f"Unknown symbol: {symbol}")

    q = select(DailyPrice).where(DailyPrice.stock_id == stock.id)
    if range != "max":
        last = db.scalar(
            select(func.max(DailyPrice.date)).where(DailyPrice.stock_id == stock.id)
        )
        if last is not None:
            q = q.where(DailyPrice.date >= last - timedelta(days=_RANGE_DAYS[range]))
    bars = db.scalars(q.order_by(DailyPrice.date.asc())).all()

    candles = [
        Candle(
            time=b.date.isoformat(),
            open=b.open,
            high=b.high,
            low=b.low,
            close=b.close,
            volume=b.volume,
        )
        for b in bars
    ]
    return CandleSeries(symbol=stock.symbol, range=range, count=len(candles), candles=candles)


def _fundamentals(db: Session, stock: Stock) -> Fundamentals:
    """Fundamentals from the latest screener snapshot, falling back to yfinance financials."""
    scr_snap = db.scalar(
        select(StockSnapshot)
        .where(
            StockSnapshot.stock_id == stock.id,
            StockSnapshot.source == "screener",
            StockSnapshot.screener.isnot(None),
        )
        .order_by(StockSnapshot.captured_at.desc(), StockSnapshot.id.desc())
        .limit(1)
    )
    if scr_snap and scr_snap.screener:
        s = scr_snap.screener
        return Fundamentals(
            annual=s.get("profit_loss") or {},
            quarterly=s.get("quarterly_results") or {},
            ratios=s.get("ratios") or {},
            shareholding=s.get("shareholding") or {},
            about=s.get("about"),
        )

    # Fallback: latest yfinance snapshot financials.
    yf = db.scalar(
        select(StockSnapshot)
        .where(StockSnapshot.stock_id == stock.id, StockSnapshot.source == "yfinance")
        .order_by(StockSnapshot.captured_at.desc(), StockSnapshot.id.desc())
        .limit(1)
    )
    if yf:
        return Fundamentals(
            annual=yf.financials or {},
            quarterly=yf.quarterly_financials or {},
            ratios={},
            shareholding={},
            about=(yf.info or {}).get("longBusinessSummary") if yf.info else None,
        )
    return Fundamentals()


@router.get("/backtest")
def backtest(
    benchmark: str = Query(DEFAULT_BENCHMARK),
    db: Session = Depends(get_db),
):
    """Point-in-time event study: forward/excess returns of every scored cohort
    member, grouped by confidence tier, LCB/composite quintile and consensus
    recommendation, plus Spearman ICs. Computed on demand (512-stock cohort is
    cheap); unpriced names are reported, never dropped."""
    return run_event_study(db, benchmark=benchmark)
