"""
ORM models for the IPO Analyzer living engine.

Design principles:
- `stocks` is the identity table (one row per tradable symbol).
- `stock_snapshots` is an immutable, append-only time series of scraped data,
  each row keyed by a `content_hash` of its meaningful payload. Identical data
  is never stored twice, and we always know when a stock's data actually changed.
- `analyses` records a persona verdict tied to the `data_hash` it was based on,
  so staleness is a pure comparison against the latest snapshot hash.
- `composite_scores` is derived and recomputed on demand.
- `job_runs` gives the admin dashboard full observability of the engine.
- `stock_insights` is the seed of the memory/knowledge layer (populated later).
"""

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base

# JSONB on Postgres (indexable), plain JSON elsewhere — keeps the schema portable.
JSONType = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Stock(Base):
    __tablename__ = "stocks"
    __table_args__ = (
        # GIN index enables fast JSONB membership queries: universe @> '["ipo_2026"]'
        Index("ix_stocks_universe_gin", "universe", postgresql_using="gin"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    isin: Mapped[str | None] = mapped_column(String(16), index=True)   # universal cross-source identity key
    company_name: Mapped[str | None] = mapped_column(String(512))
    cap_category: Mapped[str | None] = mapped_column(String(16))       # large / mid / small (per AMFI/SEBI)
    exchange: Mapped[str | None] = mapped_column(String(16))          # NSE / BSE
    nse_symbol: Mapped[str | None] = mapped_column(String(64))
    yf_symbol: Mapped[str | None] = mapped_column(String(64))         # e.g. ATHERENERG.NS
    sector: Mapped[str | None] = mapped_column(String(128))
    industry: Mapped[str | None] = mapped_column(String(256))
    listing_date: Mapped[str | None] = mapped_column(String(32))
    issue_price: Mapped[float | None] = mapped_column(Float)
    ipo_mcap_cr: Mapped[float | None] = mapped_column(Float)
    screener_url: Mapped[str | None] = mapped_column(String(512))

    # Universe membership tags, e.g. ["ipo_2025", "ipo_2026", "nse_smallcap"].
    universe: Mapped[list] = mapped_column(JSONType, default=list)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)

    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    snapshots: Mapped[list["StockSnapshot"]] = relationship(
        back_populates="stock", cascade="all, delete-orphan"
    )
    analyses: Mapped[list["Analysis"]] = relationship(
        back_populates="stock", cascade="all, delete-orphan"
    )
    composite_scores: Mapped[list["CompositeScore"]] = relationship(
        back_populates="stock", cascade="all, delete-orphan"
    )
    insights: Mapped[list["StockInsight"]] = relationship(
        back_populates="stock", cascade="all, delete-orphan"
    )


class StockSnapshot(Base):
    __tablename__ = "stock_snapshots"
    __table_args__ = (
        UniqueConstraint("stock_id", "content_hash", name="uq_snapshot_stock_hash"),
        Index("ix_snapshot_stock_captured", "stock_id", "captured_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id", ondelete="CASCADE"), index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    source: Mapped[str] = mapped_column(String(32), default="yfinance")   # yfinance / screener
    fetch_status: Mapped[str | None] = mapped_column(String(32))
    data_quality: Mapped[str | None] = mapped_column(String(32))          # full/partial/limited/minimal/none
    content_hash: Mapped[str] = mapped_column(String(64), index=True)

    # Raw payloads (immutable facts).
    info: Mapped[dict | None] = mapped_column(JSONType)
    financials: Mapped[dict | None] = mapped_column(JSONType)              # yfinance annual
    quarterly_financials: Mapped[dict | None] = mapped_column(JSONType)    # yfinance quarterly
    balance_sheet: Mapped[dict | None] = mapped_column(JSONType)
    cashflow: Mapped[dict | None] = mapped_column(JSONType)
    history_summary: Mapped[dict | None] = mapped_column(JSONType)
    ipo_data: Mapped[dict | None] = mapped_column(JSONType)
    screener: Mapped[dict | None] = mapped_column(JSONType)                # screener.in scrape (multi-year, quarterly, ROCE, shareholding)

    # Extracted convenience columns for fast querying/screening.
    current_price: Mapped[float | None] = mapped_column(Float)
    market_cap: Mapped[float | None] = mapped_column(Float)
    pe_ratio: Mapped[float | None] = mapped_column(Float)
    roe: Mapped[float | None] = mapped_column(Float)
    debt_to_equity: Mapped[float | None] = mapped_column(Float)
    revenue_growth: Mapped[float | None] = mapped_column(Float)

    stock: Mapped["Stock"] = relationship(back_populates="snapshots")


class Analysis(Base):
    __tablename__ = "analyses"
    __table_args__ = (
        Index("ix_analysis_stock_persona", "stock_id", "persona"),
        Index("ix_analysis_data_hash", "data_hash"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id", ondelete="CASCADE"), index=True)
    persona: Mapped[str] = mapped_column(String(64), index=True)
    model: Mapped[str | None] = mapped_column(String(64))
    prompt_version: Mapped[str | None] = mapped_column(String(32))

    # Which data this verdict was based on (staleness key).
    snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("stock_snapshots.id", ondelete="SET NULL")
    )
    data_hash: Mapped[str | None] = mapped_column(String(64))

    score: Mapped[int | None] = mapped_column(Integer)
    recommendation: Mapped[str | None] = mapped_column(String(16))   # BUY/HOLD/AVOID
    investment_thesis: Mapped[str | None] = mapped_column(Text)
    key_strengths: Mapped[list | None] = mapped_column(JSONType)
    key_risks: Mapped[list | None] = mapped_column(JSONType)
    red_flags: Mapped[list | None] = mapped_column(JSONType)
    detailed_analysis: Mapped[str | None] = mapped_column(Text)
    metrics_evaluated: Mapped[dict | None] = mapped_column(JSONType)

    analyzed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    total_cost_usd: Mapped[float | None] = mapped_column(Float)
    usage: Mapped[dict | None] = mapped_column(JSONType)

    stock: Mapped["Stock"] = relationship(back_populates="analyses")


class CompositeScore(Base):
    __tablename__ = "composite_scores"

    id: Mapped[int] = mapped_column(primary_key=True)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id", ondelete="CASCADE"), index=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    composite_score: Mapped[float | None] = mapped_column(Float, index=True)
    persona_scores: Mapped[dict | None] = mapped_column(JSONType)
    consensus_recommendation: Mapped[str | None] = mapped_column(String(16))
    recommendation_counts: Mapped[dict | None] = mapped_column(JSONType)
    analysis_coverage: Mapped[int | None] = mapped_column(Integer)
    total_personas: Mapped[int | None] = mapped_column(Integer)
    scoring_method: Mapped[str | None] = mapped_column(String(64))

    stock: Mapped["Stock"] = relationship(back_populates="composite_scores")


class JobRun(Base):
    """Every engine action (scrape / fetch / analyze / score / refresh) is logged here."""

    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_type: Mapped[str] = mapped_column(String(64), index=True)
    target: Mapped[str | None] = mapped_column(String(128))   # symbol / "all" / universe tag
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stats: Mapped[dict | None] = mapped_column(JSONType)
    error: Mapped[str | None] = mapped_column(Text)


class StockInsight(Base):
    """Distilled, reusable takeaways — the seed of the memory/knowledge layer."""

    __tablename__ = "stock_insights"

    id: Mapped[int] = mapped_column(primary_key=True)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id", ondelete="CASCADE"), index=True)
    persona: Mapped[str | None] = mapped_column(String(64))
    insight: Mapped[str] = mapped_column(Text)
    tags: Mapped[list | None] = mapped_column(JSONType)
    source_analysis_id: Mapped[int | None] = mapped_column(
        ForeignKey("analyses.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    stock: Mapped["Stock"] = relationship(back_populates="insights")
