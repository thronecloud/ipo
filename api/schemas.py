"""Pydantic v2 response models — the exact JSON shapes in DASHBOARDS_SPEC.md."""

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
    consensus_recommendation: str | None = None
    analysis_coverage: int | None = None
    per_persona: dict[str, PersonaVerdict] = {}
    current_price: float | None = None
    market_cap_cr: float | None = None
    pe_ratio: float | None = None
    roe: float | None = None
    debt_to_equity: float | None = None
    revenue_growth: float | None = None
    ipo_return_pct: float | None = None
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
    current_price: float | None = None
    market_cap_cr: float | None = None
    pe_ratio: float | None = None
    roe: float | None = None
    debt_to_equity: float | None = None
    revenue_growth: float | None = None
    ipo_return_pct: float | None = None
    data_quality: str | None = None
    as_of: datetime | None = None


class Composite(BaseModel):
    composite_score: float | None = None
    consensus_recommendation: str | None = None
    recommendation_counts: dict[str, int] = {}
    analysis_coverage: int | None = None
    total_personas: int | None = None


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
