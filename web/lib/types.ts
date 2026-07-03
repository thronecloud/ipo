// ── API contract types (mirror DASHBOARDS_SPEC.md) ─────────────────

export type Recommendation = "BUY" | "HOLD" | "AVOID";
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
  consensus_recommendation: Recommendation | null;
  analysis_coverage: number;
  per_persona: Record<string, PerPersonaVerdict>;
  current_price: number | null;
  market_cap_cr: number | null;
  pe_ratio: number | null;
  roe: number | null;
  debt_to_equity: number | null;
  revenue_growth: number | null;
  ipo_return_pct: number | null;
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
  current_price: number | null;
  market_cap_cr: number | null;
  pe_ratio: number | null;
  roe: number | null;
  debt_to_equity: number | null;
  revenue_growth: number | null;
  ipo_return_pct: number | null;
  data_quality: DataQuality | null;
  as_of: string | null;
}

export interface CompositeSummary {
  composite_score: number | null;
  consensus_recommendation: Recommendation | null;
  recommendation_counts: Partial<Record<Recommendation, number>>;
  analysis_coverage: number;
  total_personas: number;
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

export type JobType = "discover" | "refresh" | "enrich" | "analyze" | "score";

export interface JobRunResponse {
  launched: boolean;
  job: string;
}
