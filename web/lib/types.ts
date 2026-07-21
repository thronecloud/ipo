// ── API contract types (mirror docs/DASHBOARDS_SPEC.md) ─────────────────

export type Recommendation = "BUY" | "HOLD" | "AVOID";
export type ConfidenceTier = "high" | "high_bearish" | "moderate" | "provisional" | "mixed";
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
  last_analyzed_at: string | null; // max analyzed_at over its analyses
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
  // Provenance: counts per prompt_version / model behind this composite. A score
  // mixing versions or models is not comparable across stocks; the UI marks it.
  prompt_versions: Record<string, number>;
  models_used: Record<string, number>;
  is_homogeneous: boolean | null;
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

export interface SchedulerJob {
  id: string;
  description: string;
  job_type: string | null;
  cadence: string;
  trigger_kind: "cron" | "interval";
  last_run: JobRun | null;
  next_expected: string | null;
  missed: boolean | null;
  recent_runs: JobRun[];
}

export interface SloOffender {
  label: string; // stock / index symbol, or feed name
  lag: number | null; // freshness lag in the SLO's unit; null = never fetched
  missing: boolean; // true when the member has no data at all
}

export interface SloReport {
  dataset: string;
  description: string;
  target: string; // human target, e.g. "≤ 1 trading day"
  unit: "trading_days" | "days";
  objective_pct: number;
  population: number;
  compliant: number;
  compliance_pct: number | null; // null when population is empty (n/a)
  worst_lag: number | null;
  missing: number;
  breached: boolean;
  offenders: SloOffender[];
}

export interface SchedulerOverview {
  now: string;
  jobs: SchedulerJob[];
  slos: SloReport[];
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

export interface TrustRow {
  source: string;
  fact: string;
  agreements: number;
  comparisons: number;
  agreement_rate: number | null;
  window_start: string | null;
}

export interface ReconciliationDiscrepancy {
  symbol: string;
  company_name: string | null;
  fact: string;
  fact_key: string;
  source_a: string;
  value_a: number | null;
  source_b: string;
  value_b: number | null;
  divergence_pct: number | null;
  detected_at: string | null;
}

export interface ReconciliationOverview {
  last_run_at: string | null;
  stocks: number;
  compared: number;
  agreements: number;
  discrepancies: number;
  new_discrepancies: number;
  resolved: number;
  stale: number;
  missing: number;
  open_count: number;
  trust: TrustRow[];
  open: ReconciliationDiscrepancy[];
}

export interface JobRunResponse {
  launched: boolean;
  job: string;
}

// ── Backtest (point-in-time event study) ─────────────────────────
// All returns are percentages already (3.95 = +3.95%); null = horizon not
// yet computable. Horizon-keyed dicts arrive JSON-stringified: {"5": …}.

// Whether a bootstrap interval clears its null hypothesis. Zero for excess /
// spread / rank correlation; a coin flip for hit rate.
export type Verdict = "positive" | "negative" | "indistinguishable from zero";

// Block-bootstrap CI over the cohort's stocks (the independent unit). `point`
// is the raw estimate; [ci_low, ci_high] is a 95% percentile band; `n` is the
// number of stocks it rests on. null in place of a CI means the cohort was too
// small to bootstrap honestly.
export interface CI {
  point: number;
  ci_low: number;
  ci_high: number;
  verdict: Verdict;
  n: number;
}

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
  // Net-of-friction twins (85 bps/side charged on entry and exit).
  net_returns: Record<string, number | null>;
  net_excess: Record<string, number | null>;
  latest_date: string | null;
  latest_price: number | null;
  return_to_date: number | null;
  excess_to_date: number | null;
  net_return_to_date: number | null;
  net_excess_to_date: number | null;
  // Execution reality: how the fill was resolved and the pick's liquidity.
  entry_basis: "open" | "close" | null; // next-open, or close where no open exists
  entry_delay_days: number; // trading days entry slipped past circuit locks
  adv: number | null; // trailing median daily traded value (₹)
  thin: boolean; // below the ADV floor — returns may be unrealizable in size
  unenterable: boolean; // circuit-locked through the whole entry window
  lcb_quintile?: number | null; // 1..5, 5 = best (priced cohort only)
  composite_quintile?: number | null;
}

export interface BacktestBucket {
  n: number;
  priced: number;
  thin: number; // names in the bucket flagged thin (below the ADV floor)
  mean_excess: Record<string, number | null>;
  median_excess: Record<string, number | null>;
  hit_rate: Record<string, number | null>; // % of names beating the benchmark
  mean_return: Record<string, number | null>;
  // Net-of-friction twin of mean excess (85 bps/side, both legs).
  net_mean_excess: Record<string, number | null>;
  // Bootstrap CIs for the hypothesis-bearing stats (vs zero / vs coin flip).
  mean_excess_ci: Record<string, CI | null>;
  net_mean_excess_ci: Record<string, CI | null>;
  hit_rate_ci: Record<string, CI | null>;
}

// Cohort completeness: how many scored names actually got measured, why the
// rest didn't, and the presumed fate + conservative policy return of the
// vanished ones. Additive accounting — the measurement math for measured stocks
// is unchanged.
export interface CohortBlock {
  scored: number;
  priceable: number; // has >=1 price bar
  measured: number; // contributed >=1 forward-horizon return
  // Thin overlay (not an exclusion): measured names below the ADV floor.
  thin: number;
  thin_symbols: string[];
  adv_floor: number; // ₹ median daily traded value below which a pick is thin
  excluded: {
    no_bars: number;
    unenterable: number; // circuit-locked through the entry window
    bars_predate_view: number;
    insufficient_forward: number;
  };
  excluded_symbols: {
    no_bars: string[];
    unenterable: string[];
    bars_predate_view: string[];
    insufficient_forward: string[];
  };
  presumed_outcomes: { delisted: number; merged: number; unknown: number };
  presumed_symbols: { delisted: string[]; merged: string[]; unknown: string[] };
  policy: {
    delisting_return: number; // the documented conservative loss constant
    applied_constant: number; // delisted names assigned the constant
    applied_actual_last: number; // delisted names using their real last-price return
    excluded_merged: number; // merged: excluded-with-reason, no synthetic return
    excluded_unknown: number; // unknown: excluded-with-reason
    symbols: {
      constant: string[];
      actual_last: string[];
      merged: string[];
      unknown: string[];
    };
  };
}

// A per-sector IC: either a bootstrap CI, or a refusal when the sector has too
// few measured names to correlate honestly.
export type SectorIc = CI | { insufficient: true; n: number };

export function icInsufficient(ic: SectorIc): ic is { insufficient: true; n: number } {
  return (ic as { insufficient?: true }).insufficient === true;
}

// Cross-sectional factor attribution over the measured cohort at one horizon.
// Excess returns are regressed on [intercept, centred log size, sector dummies];
// the intercept is the residual alpha and the residuals are the tilt-stripped
// excess returns. raw_ic (composite vs excess) beside residual_ic (composite vs
// residual) says whether the score picks or just tilts.
export interface AttributionBlock {
  horizon: number; // trading days the regression was run at
  n: number; // stocks in the regression (score + market cap + excess return)
  measured: number; // stocks in the per-sector IC cohort (score + return)
  insufficient: boolean; // true -> too few names; every estimate below is null
  reason: string | null;
  dummies_dropped: boolean; // design still singular after pooling -> sectors shed
  size_dropped: boolean; // log size degenerate -> size term shed
  reference_sector: string | null; // the held-out baseline dummy
  modeled_sectors: string[]; // sectors that carry a dummy (excludes the reference)
  pooled_into_other: string[]; // named sectors folded into "other"
  alpha: number | null; // residual alpha (the intercept)
  size_loading: number | null; // per-unit-log-size tilt
  sector_loadings: Record<string, number>; // per modeled sector, vs the reference
  r2: number | null;
  raw_ic: CI | null; // composite vs excess return, over the regression cohort
  residual_ic: CI | null; // composite vs tilt-stripped residual, same cohort
  per_sector_ic: Record<string, SectorIc>;
}

export interface BacktestStudy {
  benchmark: string;
  benchmark_bars: number;
  horizons: number[]; // trading days, e.g. [5, 21, 63, 126]
  cohort_size: number;
  priced: number;
  unpriced_symbols: string[];
  // Execution-reality terms the study was run under.
  friction_bps: number; // one-way, charged on both legs
  adv_floor: number; // ₹ median daily traded value below which a pick is thin
  entry_window: number; // trading days allowed to find a tradeable session
  entry_basis: string; // "next_open"
  stocks: BacktestStock[];
  by_tier: Record<string, BacktestBucket>;
  by_recommendation: Record<string, BacktestBucket>;
  by_lcb_quintile: Record<string, BacktestBucket>;
  by_composite_quintile: Record<string, BacktestBucket>;
  ic: {
    composite: Record<string, number | null>; // Spearman rho, -1..1
    lcb: Record<string, number | null>;
  };
  ic_ci: {
    composite: Record<string, CI | null>; // bootstrap band on the rank correlation
    lcb: Record<string, CI | null>;
  };
  // Skill vs tilt: excess returns regressed on size + sector at one horizon, so
  // the composite's IC can be read raw AND with the tilt stripped out.
  attribution: AttributionBlock;
  overall: BacktestBucket;
  cohort: CohortBlock;
  delisting_return_policy: number;
  // The SAME overall stat recomputed with presumed-delisted names folded back in
  // via the policy. null when no policy row applied.
  overall_with_policy: BacktestBucket | null;
  // The SAME overall stat over the non-thin subset (gross AND net), so the
  // headline can be read with and without unrealizable-in-size names. null when
  // no thin pick exists.
  overall_ex_thin: BacktestBucket | null;
}

// ── Per-persona backtest (equity curves + summary stats) ─────────
// One trading-day-offset point of an equal-weighted BUY-pick portfolio,
// rebased to 100 at entry, alongside the same picks' benchmark path.
export interface EquityPoint {
  t: number; // trading days since entry
  portfolio: number; // rebased to 100 (gross)
  net_portfolio: number; // the same basket net of round-trip friction
  benchmark: number | null; // rebased to 100
  n: number; // basket size at this offset
  // p5/p95 band of the portfolio mean from resampling the picks (rebased to
  // 100). null where the basket is too small to bootstrap.
  p5: number | null;
  p95: number | null;
}

// Forward performance of one signal's top-conviction (BUY) picks.
export interface PortfolioResult {
  n_buy: number;
  n_avoid: number;
  n_buy_priced: number;
  n_thin: number; // BUY picks flagged thin (below the ADV floor)
  stats: BacktestBucket; // over the BUY picks
  // BUY-pick stats over the non-thin subset. null when no BUY pick is thin.
  stats_ex_thin: BacktestBucket | null;
  spread: Record<string, number | null>; // BUY excess minus AVOID excess, per horizon
  spread_ci: Record<string, CI | null>; // bootstrap band on the spread (vs zero)
  curve: EquityPoint[];
}

export interface PersonaResult extends PortfolioResult {
  persona: string; // council slug
}

export interface SubsetResult extends PortfolioResult {
  personas: string[]; // the chosen subset, canonical order
}

export interface PersonaStudy {
  benchmark: string;
  benchmark_bars: number;
  horizons: number[];
  curve_offsets: number[];
  cohort_size: number;
  cohort: CohortBlock;
  delisting_return_policy: number;
  council: string[];
  personas: PersonaResult[];
  subset: SubsetResult;
}

// ── Walk-forward vintages + prompt-era attribution ────────────────
// The event study rolled through time: one window per formation date, each with
// mean excess / hit rate / IC over the hold horizon (+ bootstrap CIs), split by
// the dominant prompt_version and model era, plus an IC-decay curve.

// One era's slice within a window. `insufficient` true -> too few measured names
// to print a number; only era_n / n are meaningful there.
export interface EraSlice {
  era_n: number; // stocks in this era's cohort for the window
  n: number; // measured (contributed a hold-horizon return)
  insufficient: boolean;
  mean_excess?: number | null;
  mean_excess_ci?: CI | null;
  net_mean_excess?: number | null;
  net_mean_excess_ci?: CI | null;
  hit_rate?: number | null;
  hit_rate_ci?: CI | null;
  ic?: number | null;
  ic_ci?: CI | null;
}

export interface VintageWindow {
  date: string; // formation date "YYYY-MM-DD"
  n_cohort: number; // stocks whose latest view existed as of this date
  n: number; // measured at the hold horizon
  mean_excess: number | null;
  mean_excess_ci: CI | null;
  net_mean_excess: number | null;
  net_mean_excess_ci: CI | null;
  hit_rate: number | null;
  hit_rate_ci: CI | null;
  ic: number | null;
  ic_ci: CI | null;
  eras: {
    prompt_version: Record<string, EraSlice>;
    model: Record<string, EraSlice>;
  };
  ic_by_horizon: Record<string, number | null>;
}

export interface IcDecayPoint {
  horizon: number; // trading days
  ic: number | null; // mean IC across windows at this horizon
  ic_ci: CI | null; // bootstrap band over the windows
  n_windows: number;
}

export interface VintageStudy {
  benchmark: string;
  benchmark_bars: number;
  step_days: number;
  hold_days: number;
  decay_horizons: number[];
  min_era_n: number;
  window_dates: string[];
  windows: VintageWindow[];
  ic_decay: IcDecayPoint[];
}
