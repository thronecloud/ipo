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

from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    Date,
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
    # Provenance of the imputable identity fields: measured (yfinance/amfi) vs
    # imputed (screener breadcrumb) vs unknown (predates provenance tracking).
    sector_source: Mapped[str | None] = mapped_column(String(16))
    industry_source: Mapped[str | None] = mapped_column(String(16))
    isin_source: Mapped[str | None] = mapped_column(String(16))
    listing_date: Mapped[str | None] = mapped_column(String(32))
    issue_price: Mapped[float | None] = mapped_column(Float)
    ipo_mcap_cr: Mapped[float | None] = mapped_column(Float)
    screener_url: Mapped[str | None] = mapped_column(String(512))

    # Universe membership tags, e.g. ["ipo_2025", "ipo_2026", "nse_smallcap"].
    universe: Mapped[list] = mapped_column(JSONType, default=list)
    # active / new / unfetchable / stale (auto-parked after repeated fetch failures)
    # / upcoming / listed_merged / withdrawn (pre-listing pipeline states)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    # Consecutive failed fetches; reset on success. At >= 3 an active stock is
    # auto-parked as "stale" (delisting / symbol change) and an alert is sent.
    fetch_failures: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    # Last time a fetch was ATTEMPTED (updated even when the hash is unchanged and no
    # new snapshot is written) — so freshness reflects attempts, not just new data.
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

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


class DailyPrice(Base):
    """Daily OHLCV bar — the price-chart time series.

    Populated from the full yfinance history already pulled during refresh (which
    was previously summarized and discarded). Corrective upsert by (stock_id, date):
    new trading days are inserted (the upsert returns the inserted count) and provider
    corrections — e.g. a corporate-action rebase — rewrite the existing bar in place; an
    incoming NULL never degrades a stored value. Deliberately NOT exposed as an ORM
    collection on Stock — a single name can carry thousands of bars, so all access is via
    explicit date-bounded queries. FK ondelete=CASCADE handles cleanup when a stock is
    removed.
    """

    __tablename__ = "daily_prices"
    __table_args__ = (
        UniqueConstraint("stock_id", "date", name="uq_daily_price_stock_date"),
        Index("ix_daily_price_stock_date", "stock_id", "date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    stock_id: Mapped[int] = mapped_column(
        ForeignKey("stocks.id", ondelete="CASCADE"), index=True
    )
    date: Mapped[date] = mapped_column(Date)
    open: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    close: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[int | None] = mapped_column(BigInteger)


class IndexPrice(Base):
    """Daily OHLCV bar for a benchmark index (e.g. ^CNXSC Nifty Smallcap 250).

    The backtest's excess-return comparator. Corrective upsert by (symbol, date), same
    discipline as daily_prices: new trading days insert (the upsert returns the inserted
    count) and provider corrections rewrite the existing bar in place; an incoming NULL
    never degrades a stored value. Indexes are not stocks — no FK, the yfinance symbol is
    the key.
    """

    __tablename__ = "index_prices"
    __table_args__ = (
        UniqueConstraint("symbol", "date", name="uq_index_price_symbol_date"),
        Index("ix_index_price_symbol_date", "symbol", "date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32))
    date: Mapped[date] = mapped_column(Date)
    open: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    close: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[int | None] = mapped_column(BigInteger)


class DataQualityReport(Base):
    """Per-stock data-quality scorecard, one row per audit run (append-only).

    Queried "latest per stock" like snapshots. `dimensions` holds the per-dimension
    breakdown, `flags` the discrepancies/gaps found (never mutates source data — the
    engine flags, the human decides), and `missing` the concrete fillable data points
    that drive the gap-fill jobs.
    """

    __tablename__ = "data_quality_reports"
    __table_args__ = (
        Index("ix_dq_stock_checked", "stock_id", "checked_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    stock_id: Mapped[int] = mapped_column(
        ForeignKey("stocks.id", ondelete="CASCADE"), index=True
    )
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    overall_score: Mapped[float | None] = mapped_column(Float, index=True)  # 0-100
    grade: Mapped[str | None] = mapped_column(String(2))                    # A/B/C/D/F
    dimensions: Mapped[dict | None] = mapped_column(JSONType)               # {dim: {score,status,detail}}
    flags: Mapped[list | None] = mapped_column(JSONType)                    # [{type,severity,detail}]
    missing: Mapped[list | None] = mapped_column(JSONType)                  # ["screener","prices",...]


class Analysis(Base):
    __tablename__ = "analyses"
    __table_args__ = (
        # Natural key: one verdict per (stock, persona, data, model). A force re-run
        # of the same data with the same model REFRESHES the row (upsert in
        # save_analysis), never duplicates it. NULL data_hash/model rows are exempt
        # (Postgres treats NULLs as distinct) — no hash means no dedup identity.
        UniqueConstraint("stock_id", "persona", "data_hash", "model",
                         name="uq_analysis_natural_key"),
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


class AnalysisFailure(Base):
    """Dead-letter ledger for persona analysis: one row per failing
    (stock, persona, data_hash). Each failed attempt increments `failures`;
    at the engine's threshold the pair is skipped by find_work — for THAT hash
    only, so changed fundamentals automatically re-qualify the pair. A later
    success deletes the row."""

    __tablename__ = "analysis_failures"
    __table_args__ = (
        UniqueConstraint("stock_id", "persona", "data_hash",
                         name="uq_analysis_failure_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    stock_id: Mapped[int] = mapped_column(
        ForeignKey("stocks.id", ondelete="CASCADE"), index=True
    )
    persona: Mapped[str] = mapped_column(String(64))
    data_hash: Mapped[str] = mapped_column(String(64))
    failures: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    last_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class CompositeScore(Base):
    __tablename__ = "composite_scores"
    __table_args__ = (
        # Exactly one composite per stock — makes recompute an atomic upsert and
        # kills the concurrent delete-then-insert duplicate-row race.
        UniqueConstraint("stock_id", name="uq_composite_stock"),
    )

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

    # Confidence layer (LEAK#1) — defined on the independent axis subspace, not raw stdev.
    axis_scores: Mapped[dict | None] = mapped_column(JSONType)          # {core,growth,value,independent}
    confidence_tier: Mapped[str | None] = mapped_column(String(16))     # high/moderate/provisional/mixed
    score_stderr_eff: Mapped[float | None] = mapped_column(Float)       # dispersion across independent axes
    lcb: Mapped[float | None] = mapped_column(Float, index=True)        # lower-confidence-bound = rank key
    factor_version: Mapped[str | None] = mapped_column(String(16))

    # What produced this composite: count per value across the contributing analyses
    # (latest per persona), e.g. {"v4": 6, "v1": 4}. factor_version is the scoring-axis
    # version — a different thing. A NULL prompt_version/model counts as "unknown".
    prompt_versions: Mapped[dict | None] = mapped_column(JSONType)
    models_used: Mapped[dict | None] = mapped_column(JSONType)

    stock: Mapped["Stock"] = relationship(back_populates="composite_scores")


class CompositeScoreHistory(Base):
    """Append-only point-in-time record of the composite, one row per (stock, as_of_date).

    The `composite_scores` table is upsert-pinned to the CURRENT score per stock (its
    unique constraint is correct for "latest"). This separate history sink preserves the
    time series a backtest needs — and its cost is irreversible if deferred: every day
    without it is a permanently unrecoverable cohort.

    `information_date` = max(analyzed_at) of the contributing analyses — the date the view
    actually FORMED (distinct from `as_of_date`, when it was computed). Point-in-time
    backtests freeze cohorts on `information_date` (no lookahead). Written for stocks of
    ANY status (survivorship: dead names keep their history — never filtered out).
    """

    __tablename__ = "composite_score_history"
    __table_args__ = (
        UniqueConstraint("stock_id", "as_of_date", name="uq_score_history_stock_day"),
        Index("ix_score_history_stock_asof", "stock_id", "as_of_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    stock_id: Mapped[int] = mapped_column(
        ForeignKey("stocks.id", ondelete="CASCADE"), index=True
    )
    as_of_date: Mapped[date] = mapped_column(Date)                 # when computed (daily point)
    information_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # when the view formed
    composite_score: Mapped[float | None] = mapped_column(Float)
    persona_scores: Mapped[dict | None] = mapped_column(JSONType)
    axis_scores: Mapped[dict | None] = mapped_column(JSONType)
    confidence_tier: Mapped[str | None] = mapped_column(String(16))
    score_stderr_eff: Mapped[float | None] = mapped_column(Float)
    lcb: Mapped[float | None] = mapped_column(Float)
    factor_version: Mapped[str | None] = mapped_column(String(16))
    consensus_recommendation: Mapped[str | None] = mapped_column(String(16))
    analysis_coverage: Mapped[int | None] = mapped_column(Integer)

    # Count per value across the analyses behind this point-in-time composite.
    prompt_versions: Mapped[dict | None] = mapped_column(JSONType)
    models_used: Mapped[dict | None] = mapped_column(JSONType)


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


class CorporateAnnouncement(Base):
    """A corporate filing/announcement pulled directly from an exchange feed.

    Append-only. The raw exchange symbol is always kept (announcements arrive from
    NSE/BSE keyed by their own ticker, not our internal id); `stock_id` is resolved
    to our universe where the symbol matches and left NULL otherwise — a non-universe
    company's announcement is still a fact worth storing. Dedup is on
    (exchange, dedup_hash): the source announcement id when the feed gives one, else a
    content hash, so re-running a daily poll over the same window never duplicates rows.
    """

    __tablename__ = "corporate_announcements"
    __table_args__ = (
        UniqueConstraint("exchange", "dedup_hash", name="uq_corp_ann_exchange_hash"),
        Index("ix_corp_ann_symbol", "symbol"),
        Index("ix_corp_ann_announced", "announced_at"),
        Index("ix_corp_ann_stock", "stock_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    stock_id: Mapped[int | None] = mapped_column(
        ForeignKey("stocks.id", ondelete="SET NULL")
    )
    symbol: Mapped[str | None] = mapped_column(String(64))     # raw exchange ticker/scrip
    exchange: Mapped[str] = mapped_column(String(8))           # NSE / BSE
    headline: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(256))
    attachment_url: Mapped[str | None] = mapped_column(String(1024))
    announced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    announcement_id: Mapped[str | None] = mapped_column(String(128))  # source-native id when present
    dedup_hash: Mapped[str] = mapped_column(String(64))
    raw: Mapped[dict | None] = mapped_column(JSONType)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CorporateFiling(Base):
    """A report/document downloaded to disk for a stock in our active universe.

    Populated by the autonomous report fetcher off results/annual-report announcements
    and the NSE annual-reports endpoint. `source_url` is the hard idempotency key per
    stock — a URL already fetched is never downloaded again — and `sha256` is the
    content-dedup key (the same PDF republished under a new URL is recognised and not
    stored twice). `status` records the outcome so a run stopped by the size cap or the
    per-run download budget is observable rather than silently missing.
    """

    __tablename__ = "corporate_filings"
    __table_args__ = (
        UniqueConstraint("stock_id", "source_url", name="uq_filing_stock_url"),
        Index("ix_filing_stock", "stock_id"),
        Index("ix_filing_sha256", "sha256"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id", ondelete="CASCADE"))
    symbol: Mapped[str | None] = mapped_column(String(64))
    filing_type: Mapped[str | None] = mapped_column(String(64))   # results / annual_report / other
    period: Mapped[str | None] = mapped_column(String(64))
    source_url: Mapped[str] = mapped_column(String(1024))
    local_path: Mapped[str | None] = mapped_column(String(512))
    sha256: Mapped[str | None] = mapped_column(String(64))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str | None] = mapped_column(String(32))        # downloaded / duplicate / skipped_cap / skipped_budget / failed
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BulkDeal(Base):
    """A single bulk or block deal reported by the exchange after market close.

    Append-only. Symbol is resolved to our universe where it matches, NULL otherwise.
    Dedup on `dedup_hash` (deal_date + symbol + client + side + quantity + source): the
    daily large-deal snapshot re-serves the same day's deals on every poll, so the hash
    keeps a re-run idempotent.
    """

    __tablename__ = "bulk_deals"
    __table_args__ = (
        UniqueConstraint("dedup_hash", name="uq_bulk_deal_hash"),
        Index("ix_bulk_deal_symbol", "symbol"),
        Index("ix_bulk_deal_date", "deal_date"),
        Index("ix_bulk_deal_stock", "stock_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    stock_id: Mapped[int | None] = mapped_column(
        ForeignKey("stocks.id", ondelete="SET NULL")
    )
    symbol: Mapped[str | None] = mapped_column(String(64))
    exchange: Mapped[str] = mapped_column(String(8), default="NSE")
    deal_date: Mapped[date | None] = mapped_column(Date)
    client_name: Mapped[str | None] = mapped_column(String(512))
    buy_sell: Mapped[str | None] = mapped_column(String(8))       # BUY / SELL
    quantity: Mapped[int | None] = mapped_column(BigInteger)
    avg_price: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(8))               # bulk / block
    dedup_hash: Mapped[str] = mapped_column(String(64))
    raw: Mapped[dict | None] = mapped_column(JSONType)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ShareholdingPattern(Base):
    """A quarter's shareholding split for a stock, from the NSE shareholding feed.

    One row per (stock, period_end) — the natural quarterly grain — so a weekly sweep
    that re-sees an already-stored quarter is a no-op. Percentages are nullable (a feed
    may omit a category); `raw` keeps the untouched source record for anything we don't
    lift into a column.
    """

    __tablename__ = "shareholding_patterns"
    __table_args__ = (
        UniqueConstraint("stock_id", "period_end", name="uq_shp_stock_period"),
        Index("ix_shp_stock_period", "stock_id", "period_end"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id", ondelete="CASCADE"))
    symbol: Mapped[str | None] = mapped_column(String(64))
    period_end: Mapped[date] = mapped_column(Date)
    promoter_pct: Mapped[float | None] = mapped_column(Float)
    fii_pct: Mapped[float | None] = mapped_column(Float)
    dii_pct: Mapped[float | None] = mapped_column(Float)
    public_pct: Mapped[float | None] = mapped_column(Float)
    pledged_pct: Mapped[float | None] = mapped_column(Float)
    raw: Mapped[dict | None] = mapped_column(JSONType)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


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
