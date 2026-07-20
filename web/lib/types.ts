// ── API contract types (mirror docs/DASHBOARDS_SPEC.md) ─────────────────

export type Recommendation = "BUY" | "HOLD" | "AVOID";
export type ConfidenceTier = "high" | "moderate" | "provisional" | "mixed";
export type CapCategory = "small" | "mid" | "large";
export type DataQuality = "full" | "partial" | "limited" | "minimal";
export type StockStatus = "active" | "new" | "unfetchable";

export interface PersonaMeta {
  slug: string;
  display_name: string;
  nationality: string; // "international" | "indian"
  philosophy: string;
}

export interface Meta {
  stocks_total: number;
  analyzed: number;
  universes: Record<string, number>;
  sectors: string[];
  last_job_at: string | null;
  personas: PersonaMeta[];
}

export interface PerPersonaVerdict {
  score: number | null;
  recommendation: Recommendation | null;
}

export interface StockRow {
  symbol: string;
  company_name: string;
  sector: string | null;
  cap_category: CapCategory | null;
  universe: string[];
  status: StockStatus | null;
  composite_score: number | null;
  lcb: number | null; // rank key: composite minus confidence penalty
  confidence_tier: ConfidenceTier | null;
  consensus_recommendation: Recommendation | null;
  analysis_coverage: number;
  composite_updated_at: string | null;
  per_persona: Record<string, PerPersonaVerdict>;
  current_price: number | null; // rupees
  market_cap_cr: number | null; // crore
  pe_ratio: number | null; // ratio
  roe: number | null; // percent, e.g. 39.42
  debt_to_equity: number | null; // ratio, e.g. 0.00818
  revenue_growth: number | null; // percent, e.g. 12.5
  ipo_return_pct: number | null; // percent
  data_quality: DataQuality | null;
}

export interface StockList {
  total: number;
  page: number;
  page_size: number;
  items: StockRow[];
}

export interface StockIdentity {
  symbol: string;
  company_name: string;
  isin: string | null;
  sector: string | null;
  industry: string | null;
  exchange: string | null;
  cap_category: CapCategory | null;
  universe: string[];
  listing_date: string | null;
  status: StockStatus | null;
}

export interface StockQuote {
  // Units are canonical as emitted by the API — render as-is, never rescale.
  current_price: number | null; // rupees
  market_cap_cr: number | null; // crore
  pe_ratio: number | null; // ratio
  roe: number | null; // percent, e.g. 39.42
  debt_to_equity: number | null; // ratio, e.g. 0.00818
  revenue_growth: number | null; // percent, e.g. 12.5
  ipo_return_pct: number | null; // percent
  data_quality: DataQuality | null;
  as_of: string | null;
}

export interface CompositeSummary {
  composite_score: number | null;
  consensus_recommendation: Recommendation | null;
  recommendation_counts: Partial<Record<Recommendation, number>>;
  analysis_coverage: number;
  total_personas: number;
  updated_at: string | null;
  // Confidence layer (LEAK#1): the 10 personas are ~2-3 independent signals,
  // so honesty lives here, not in "10 experts agree".
  lcb: number | null;
  confidence_tier: ConfidenceTier | null;
  score_stderr_eff: number | null;
  axis_scores: Record<string, number | null>; // core/growth/value/independent -> 0-100
  factor_version: string | null;
}

export interface CouncilVerdict {
  persona: string;
  display_name: string;
  nationality: string;
  score: number | null;
  recommendation: Recommendation | null;
  investment_thesis: string | null;
  key_strengths: string[];
  key_risks: string[];
  red_flags: string[];
  detailed_analysis: string | null;
  metrics_evaluated: Record<string, unknown> | null;
  model: string | null;
  analyzed_at: string | null;
}

export interface Fundamentals {
  annual: Record<string, Record<string, number | null>>;
  quarterly: Record<string, Record<string, number | null>>;
  ratios: Record<string, number | string | null>;
  shareholding: Record<string, Record<string, number | null>>;
  about: string | null;
}

export interface ConvictionPoint {
  computed_at: string;
  composite_score: number | null;
  consensus_recommendation: Recommendation | null;
}

export interface StockDetail {
  identity: StockIdentity;
  quote: StockQuote;
  composite: CompositeSummary;
  council: CouncilVerdict[];
  fundamentals: Fundamentals;
  conviction_history: ConvictionPoint[];
}

export type CandleRange = "1m" | "3m" | "6m" | "1y" | "3y" | "5y" | "max";

export interface Candle {
  time: string; // "YYYY-MM-DD"
  open: number | null;
  high: number | null;
  low: number | null;
  close: number | null;
  volume: number | null;
}

export interface CandleSeries {
  symbol: string;
  range: CandleRange;
  count: number;
  candles: Candle[];
}

// ── Admin ─────────────────────────────────────────────────────────

export interface CoverageRow {
  universe: string;
  tracked: number;
  full_data: number;
  analyzed: number;
  backlog?: number;
}

export interface JobRun {
  id: number;
  job_type: string;
  target: string | null;
  status: "running" | "success" | "error";
  started_at: string | null;
  finished_at: string | null;
  duration_s: number | null;
  stats: Record<string, unknown> | null;
  error: string | null;
}

export interface AdminOverview {
  stocks_total: number;
  by_status: Record<string, number>;
  snapshots: { yfinance: number; screener: number };
  analyses_total: number;
  scored: number;
  analysis_backlog: number;
  coverage: CoverageRow[];
  freshness: { fresh_24h: number; stale_7d: number; older: number };
  recent_jobs: JobRun[];
}

export interface PersonaDistribution {
  persona: string;
  display_name: string;
  count: number;
  avg_score: number | null;
  score_hist: number[]; // c0..c10
  rec_counts: Partial<Record<Recommendation, number>>;
}

export interface UsageModel {
  analyses: number;
  total_cost_usd: number;
  input_tokens: number;
  output_tokens: number;
}

export interface Usage {
  by_model: Record<string, UsageModel>;
  total_analyses: number;
  backlog: number;
}

export type JobType =
  | "discover"
  | "refresh"
  | "enrich"
  | "analyze"
  | "score"
  | "backfill"
  | "dq_audit"
  | "dq_fill";

export interface DimensionCoverage {
  avg_score: number | null;
  pct_pass: number | null;
  scored: number;
}

export interface DQStockRow {
  symbol: string;
  company_name: string | null;
  overall_score: number | null;
  grade: string | null;
  flags: string[];
  missing: string[];
}

export interface DQDiscrepancy {
  symbol: string;
  type: string;
  detail: string;
}

export interface DataQualityOverview {
  audited_at: string | null;
  audited: number;
  avg_overall: number | null;
  grades: Record<string, number>;
  dimension_coverage: Record<string, DimensionCoverage>;
  flags: Record<string, number>;
  missing: Record<string, number>;
  worst: DQStockRow[];
  discrepancies: DQDiscrepancy[];
}

export interface JobRunResponse {
  launched: boolean;
  job: string;
}

// ── Backtest (point-in-time event study) ─────────────────────────
// All returns are percentages already (3.95 = +3.95%); null = horizon not
// yet computable. Horizon-keyed dicts arrive JSON-stringified: {"5": …}.

export interface BacktestStock {
  symbol: string;
  company_name: string | null;
  status: StockStatus | null;
  composite: number | null;
  lcb: number | null;
  tier: ConfidenceTier | null;
  recommendation: Recommendation | null;
  coverage: number | null;
  information_date: string | null; // "YYYY-MM-DD"
  entry_date: string | null;
  entry_price: number | null;
  returns: Record<string, number | null>; // horizon (trading days) -> pct
  excess: Record<string, number | null>;
  latest_date: string | null;
  latest_price: number | null;
  return_to_date: number | null;
  excess_to_date: number | null;
  lcb_quintile?: number | null; // 1..5, 5 = best (priced cohort only)
  composite_quintile?: number | null;
}

export interface BacktestBucket {
  n: number;
  priced: number;
  mean_excess: Record<string, number | null>;
  median_excess: Record<string, number | null>;
  hit_rate: Record<string, number | null>; // % of names beating the benchmark
  mean_return: Record<string, number | null>;
}

export interface BacktestStudy {
  benchmark: string;
  benchmark_bars: number;
  horizons: number[]; // trading days, e.g. [5, 21, 63, 126]
  cohort_size: number;
  priced: number;
  unpriced_symbols: string[];
  stocks: BacktestStock[];
  by_tier: Record<string, BacktestBucket>;
  by_recommendation: Record<string, BacktestBucket>;
  by_lcb_quintile: Record<string, BacktestBucket>;
  by_composite_quintile: Record<string, BacktestBucket>;
  ic: {
    composite: Record<string, number | null>; // Spearman rho, -1..1
    lcb: Record<string, number | null>;
  };
  overall: BacktestBucket;
}
