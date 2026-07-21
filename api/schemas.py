"""Pydantic v2 response models — the exact JSON shapes in docs/DASHBOARDS_SPEC.md."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


# ---------- meta ----------

class PersonaMeta(BaseModel):
    slug: str
    display_name: str
    nationality: str
    philosophy: str


class Meta(BaseModel):
    stocks_total: int
    analyzed: int
    universes: dict[str, int]
    sectors: list[str]
    last_job_at: datetime | None = None
    personas: list[PersonaMeta]


# ---------- stock list ----------

class PersonaVerdict(BaseModel):
    score: int | None = None
    recommendation: str | None = None


class StockListItem(BaseModel):
    symbol: str
    company_name: str | None = None
    sector: str | None = None
    cap_category: str | None = None
    universe: list[str] = []
    status: str | None = None
    composite_score: float | None = None
    lcb: float | None = None                 # rank key: composite minus confidence penalty
    confidence_tier: str | None = None       # high/high_bearish/moderate/provisional/mixed
    consensus_recommendation: str | None = None
    analysis_coverage: int | None = None
    composite_updated_at: datetime | None = None
    last_analyzed_at: datetime | None = None   # max analyzed_at over its analyses
    per_persona: dict[str, PersonaVerdict] = {}
    current_price: float | None = None       # rupees
    market_cap_cr: float | None = None       # crore
    pe_ratio: float | None = None            # ratio
    roe: float | None = None                 # percent, e.g. 39.4
    debt_to_equity: float | None = None      # ratio, e.g. 0.098
    revenue_growth: float | None = None      # percent, e.g. 18.5
    ipo_return_pct: float | None = None      # percent
    data_quality: str | None = None


class StockList(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[StockListItem]


# ---------- stock detail ----------

class Identity(BaseModel):
    symbol: str
    company_name: str | None = None
    isin: str | None = None
    sector: str | None = None
    industry: str | None = None
    exchange: str | None = None
    cap_category: str | None = None
    universe: list[str] = []
    listing_date: str | None = None
    status: str | None = None


class Quote(BaseModel):
    current_price: float | None = None       # rupees
    market_cap_cr: float | None = None       # crore
    pe_ratio: float | None = None            # ratio
    roe: float | None = None                 # percent, e.g. 39.4
    debt_to_equity: float | None = None      # ratio, e.g. 0.098
    revenue_growth: float | None = None      # percent, e.g. 18.5
    ipo_return_pct: float | None = None      # percent
    data_quality: str | None = None
    as_of: datetime | None = None


class Composite(BaseModel):
    composite_score: float | None = None
    consensus_recommendation: str | None = None
    recommendation_counts: dict[str, int] = {}
    analysis_coverage: int | None = None
    total_personas: int | None = None
    updated_at: datetime | None = None
    # Confidence layer (LEAK#1): the 10 personas are ~2-3 independent signals,
    # so honesty lives here, not in "10 experts agree".
    lcb: float | None = None
    confidence_tier: str | None = None
    score_stderr_eff: float | None = None
    axis_scores: dict = {}
    factor_version: str | None = None
    # Provenance: counts per prompt_version / model across contributing analyses.
    # is_homogeneous is false when a composite mixes versions or models — such a
    # score is not comparable across stocks and the UI must mark it.
    prompt_versions: dict[str, int] = {}
    models_used: dict[str, int] = {}
    is_homogeneous: bool | None = None


class CouncilMember(BaseModel):
    persona: str
    display_name: str
    nationality: str
    score: int | None = None
    recommendation: str | None = None
    investment_thesis: str | None = None
    key_strengths: list = []
    key_risks: list = []
    red_flags: list = []
    detailed_analysis: str | None = None
    metrics_evaluated: dict | None = None
    model: str | None = None
    analyzed_at: datetime | None = None


class Fundamentals(BaseModel):
    annual: dict = {}
    quarterly: dict = {}
    ratios: dict = {}
    shareholding: dict = {}
    about: str | None = None


class ConvictionPoint(BaseModel):
    computed_at: datetime
    composite_score: float | None = None
    consensus_recommendation: str | None = None


class StockDetail(BaseModel):
    identity: Identity
    quote: Quote
    composite: Composite
    council: list[CouncilMember]
    fundamentals: Fundamentals
    conviction_history: list[ConvictionPoint]


class Candle(BaseModel):
    time: str  # ISO date "YYYY-MM-DD" (lightweight-charts business-day format)
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: int | None = None


class CandleSeries(BaseModel):
    symbol: str
    range: str
    count: int
    candles: list[Candle]


# ---------- admin ----------

class CoverageRow(BaseModel):
    universe: str
    tracked: int
    full_data: int
    analyzed: int
    backlog: int | None = None


class Freshness(BaseModel):
    fresh_24h: int
    stale_7d: int
    older: int


class DimensionCoverage(BaseModel):
    avg_score: float | None = None
    pct_pass: float | None = None
    scored: int = 0


class DQStockRow(BaseModel):
    symbol: str
    company_name: str | None = None
    overall_score: float | None = None
    grade: str | None = None
    flags: list[str] = []
    missing: list[str] = []


class DQDiscrepancy(BaseModel):
    symbol: str
    type: str
    detail: str


class DataQualityOverview(BaseModel):
    audited_at: datetime | None = None
    audited: int = 0
    avg_overall: float | None = None
    grades: dict[str, int] = {}
    dimension_coverage: dict[str, DimensionCoverage] = {}
    flags: dict[str, int] = {}
    missing: dict[str, int] = {}
    worst: list[DQStockRow] = []
    discrepancies: list[DQDiscrepancy] = []


class JobRow(BaseModel):
    id: int
    job_type: str
    target: str | None = None
    status: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_s: float | None = None
    stats: dict | None = None
    error: str | None = None


class AdminOverview(BaseModel):
    stocks_total: int
    by_status: dict[str, int]
    snapshots: dict[str, int]
    analyses_total: int
    scored: int
    analysis_backlog: int
    coverage: list[CoverageRow]
    freshness: Freshness
    recent_jobs: list[JobRow]


class PersonaDistribution(BaseModel):
    persona: str
    display_name: str
    count: int
    avg_score: float | None = None
    score_hist: list[int]
    rec_counts: dict[str, int]


class ModelUsage(BaseModel):
    analyses: int
    total_cost_usd: float
    input_tokens: int
    output_tokens: int


class Usage(BaseModel):
    by_model: dict[str, ModelUsage]
    total_analyses: int
    backlog: int


class JobRunRequest(BaseModel):
    job: str
    args: dict = {}


class JobRunResponse(BaseModel):
    launched: bool
    job: str
