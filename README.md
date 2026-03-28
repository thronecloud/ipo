# Indian IPO Analyzer

**AI-powered stock analysis of 200+ Indian IPOs through the lenses of 10 legendary investor personas -- from Warren Buffett to Rakesh Jhunjhunwala.**

---

## Why This Project

India's IPO market has exploded. In 2025 alone, over 200 companies listed on NSE and BSE. For retail investors, evaluating each one is overwhelming: most lack multi-year financial track records, coverage from institutional research desks is thin, and the hype cycle around listing day makes rational analysis difficult.

This project solves that problem by automating deep fundamental analysis across every recent IPO, viewed through 10 distinct investment philosophies. Instead of a single opinion, each stock receives a structured evaluation from value investors, growth investors, quantitative screeners, and India-market specialists -- producing a composite score grounded in the frameworks that have generated the best long-term returns in investing history.

The result is a Streamlit dashboard where you can filter, sort, and drill into any IPO by sector, valuation, score, or individual persona -- with full transparency into the reasoning behind every rating.

---

## Architecture

```
                                  PIPELINE ORCHESTRATOR
                                   (run_pipeline.py)
                                         |
            +----------------------------+----------------------------+
            |              |                  |                       |
        Stage 1        Stage 2           Stage 3                 Stage 4
     Scrape IPOs     Fetch Data       AI Analysis            Compute Scores
            |              |                  |                       |
   screener.in ──>   yfinance API ──>   Claude CLI ──>         Aggregation
   HTML scraper      + screener.in      10 personas x          Weighted avg
            |         enrichment         N stocks               Consensus rec
            v              v                  v                       v
   ipo_list.json   stocks/{SYM}.json  analyses/{SYM}/         scores.json
                                       {persona}.json                |
                                                                     v
                                                             Streamlit Dashboard
                                                                  (app.py)
```

All intermediate outputs are JSON files. The dashboard reads only pre-computed data -- zero API calls at runtime.

---

## Investor Personas

The system evaluates every stock through 10 carefully crafted personas, each with a distinct system prompt, scoring rubric, and set of metrics they prioritize:

| # | Persona | Philosophy | Key Focus |
|---|---------|-----------|-----------|
| 1 | **Warren Buffett** | Value investing with moats | ROE > 15%, low debt, durable competitive advantages |
| 2 | **Charlie Munger** | Multidisciplinary mental models | ROIC > 15%, earnings quality, inversion thinking |
| 3 | **Benjamin Graham** | Deep value / margin of safety | P/E < 15, P/B < 1.5, current ratio > 2.0, inherently skeptical of IPOs |
| 4 | **Peter Lynch** | Growth at a Reasonable Price | PEG < 1.0, "invest in what you know", ten-bagger potential |
| 5 | **Philip Fisher** | Quality growth / scuttlebutt | R&D intensity, management vision, 15-point qualitative checklist |
| 6 | **Joel Greenblatt** | Magic Formula (quantitative) | Earnings yield + return on capital, systematic ranking |
| 7 | **Howard Marks** | Risk-first / second-level thinking | What is priced in vs. reality, cycle awareness, asymmetric payoffs |
| 8 | **Rakesh Jhunjhunwala** | India growth conviction | India consumption boom, promoter quality, sector tailwinds |
| 9 | **Radhakishan Damani** | Conservative compounders | Fortress balance sheets, D/E < 0.3, steady 12-18% CAGR |
| 10 | **Vijay Kedia** | Small-cap multibaggers (SMILE) | Market cap < 1000 Cr, large TAM, transformation catalysts |

Each persona scores 0-10 per stock. The composite score sums all 10, yielding a total out of 100. Consensus recommendation is determined by majority vote across personas.

---

## Data Pipeline

### Stage 1 -- IPO List Scraping

```
screener.in/ipo/recent/ --> HTML parsing --> data/ipo_list.json
```

- Paginates through screener.in IPO listings, filtering by year
- Extracts company name, listing date, issue price, market cap, IPO return
- Resolves numeric BSE codes and screener IDs to NSE symbols by visiting individual company pages
- Handles multiple href patterns: `/company/SYMBOL/`, `/company/123456/`, `/company/id/12345/`

### Stage 2 -- Financial Data Fetching

```
yfinance API --> data/stocks/{SYMBOL}.json
screener.in company pages --> merged into same file
```

- Fetches `.info`, `.financials`, `.balance_sheet`, `.cashflow`, `.history` per stock
- Computes data quality scores (`full`, `partial`, `limited`, `minimal`) based on available data
- Stage 2b enriches from screener.in: P&L, balance sheet, cash flow, key ratios, shareholding, company description
- Fills yfinance gaps with screener data (P/E, ROE, ROCE, book value, market cap)
- Saves error stubs for failed fetches to avoid re-querying on subsequent runs

### Stage 3 -- AI Persona Analysis

```
stock data + persona prompt --> Claude CLI --> data/analyses/{SYMBOL}/{persona}.json
```

- Builds a structured financial summary from available data (valuation, profitability, growth, balance sheet, price history, income statement)
- Invokes `claude -p` with per-persona system prompts and `--json-schema` for structured output
- Each analysis returns: score (0-10), recommendation (BUY/HOLD/AVOID), investment thesis, key strengths, key risks, red flags, detailed analysis, and five evaluated metrics
- Retries up to 3 times per call with exponential backoff

### Stage 4 -- Composite Scoring

```
all persona analyses --> aggregation --> data/scores.json
```

- Sums persona scores into a composite (0-100)
- Determines consensus recommendation via majority vote
- Enriches with market data from multiple sources (yfinance, screener.in) with fallback chains
- Sorts and ranks all stocks for the dashboard

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Language | Python 3.10+ |
| AI Analysis | Claude CLI (`claude -p`) with structured JSON output |
| Financial Data | yfinance (NSE/BSE), screener.in (scraping) |
| Web Scraping | requests + BeautifulSoup4 |
| Dashboard | Streamlit |
| Data Layer | JSON files (flat-file cache) |
| Data Processing | pandas, NumPy |
| Orchestration | Custom pipeline runner (subprocess-based) |

---

## Getting Started

### Prerequisites

- Python 3.10+
- [Claude CLI](https://docs.anthropic.com/en/docs/claude-cli) installed and authenticated (Claude Max plan -- no API key needed)

### Installation

```bash
git clone https://github.com/<your-username>/indian-ipo-analyzer.git
cd indian-ipo-analyzer
pip install -r requirements.txt
```

### Run the Full Pipeline

```bash
# All four stages sequentially
python run_pipeline.py --stages 1,2,3,4

# With options
python run_pipeline.py --stages 1,2,3,4 --model opus --year 2025 --limit 20
```

### Run Individual Stages

```bash
# Stage 1: Scrape IPO list from screener.in
python3 -m src.fetch_ipo_list --year 2025 --pages 25

# Stage 2: Fetch financial data from yfinance
python3 -m src.fetch_stock_data

# Stage 2b: Enrich with screener.in data
python3 -m src.fetch_screener_data

# Stage 3: Run AI persona analysis (each stock x 10 personas)
python3 -m src.analyze --model opus

# Stage 3b: Compute composite scores
python3 -m src.score
```

### Analyze a Single Stock

```bash
# All personas for one stock
python3 -m src.analyze --symbol SWIGGY --model sonnet

# One persona for one stock
python3 -m src.analyze --symbol SWIGGY --persona warren_buffett --model opus
```

### Launch the Dashboard

```bash
streamlit run app.py
```

---

## Example Output

### Dashboard Overview

The main view shows a sortable, filterable table of all analyzed IPOs with composite scores, consensus recommendations, sector labels, and key financial metrics. A progress bar visualizes each stock's composite score relative to the 0-100 range.

### Per-Stock Detail View

Expanding any stock reveals:

- **Score bar chart** across all 10 personas -- immediately shows consensus vs. disagreement
- **Tabbed persona views** with full investment thesis, key strengths, key risks, red flags, and detailed multi-paragraph analysis
- **Evaluated metrics** (moat strength, management quality, financial health, valuation, growth potential) per persona

### Structured Analysis Output (per persona)

Each analysis file contains:

```json
{
  "score": 6,
  "recommendation": "HOLD",
  "investment_thesis": "...",
  "key_strengths": ["...", "...", "..."],
  "key_risks": ["...", "...", "..."],
  "red_flags": ["..."],
  "detailed_analysis": "...",
  "metrics_evaluated": {
    "moat_strength": "moderate",
    "management_quality": "good",
    "financial_health": "adequate",
    "valuation": "fair",
    "growth_potential": "good"
  }
}
```

---

## How It Works

### AI Analysis Approach

The core insight is that a single investment opinion is fragile. By evaluating each stock through 10 fundamentally different investment frameworks, the system surfaces a richer, more nuanced view:

1. **Persona-specific system prompts** encode deep domain knowledge -- each persona has a detailed investment philosophy, specific metrics they focus on, red flags they watch for, and calibrated scoring guidance. For example, Benjamin Graham is explicitly told to be skeptical of IPOs (no multi-year track record), while Vijay Kedia is primed to look for small-cap transformation stories.

2. **Structured output via JSON schema** ensures every analysis follows an identical format: integer score, enum recommendation, arrays of strengths/risks/flags, and a fixed set of evaluated metrics. This makes downstream aggregation and comparison deterministic.

3. **Financial summaries are auto-generated** from available data. The system builds a dense text summary covering valuation ratios, profitability metrics, growth rates, balance sheet data, price history, and income statement highlights -- adapting to whatever data is available for each stock.

4. **Composite scoring** aggregates the 10 individual scores (each 0-10) into a sum out of 100. Consensus recommendation uses majority vote. This naturally surfaces stocks where most frameworks agree (high or low) and highlights those with high disagreement (interesting for further manual research).

### Why Claude CLI Instead of the API

This project uses `claude -p` (the CLI tool with `--output-format json --json-schema`) rather than the Anthropic API. With a Claude Max subscription, this provides unlimited analysis runs with zero marginal cost -- critical when analyzing 200+ stocks across 10 personas (2,000+ inference calls).

---

## Data Sources

| Source | What It Provides | Access Method |
|--------|-----------------|---------------|
| **screener.in** | IPO listings, company financials (P&L, balance sheet, cash flow), key ratios, shareholding patterns, company descriptions | HTML scraping with rate limiting |
| **yfinance** | Stock info, financial statements, balance sheets, cash flow, price history | Python library (Yahoo Finance API) |
| **Claude** (via CLI) | AI-generated analysis per persona | `claude -p` with structured JSON output |

---

## Project Structure

```
indian-ipo-analyzer/
|
|-- run_pipeline.py              # Pipeline orchestrator (stages 1-4)
|-- app.py                       # Streamlit dashboard
|-- requirements.txt             # Python dependencies
|
|-- src/
|   |-- fetch_ipo_list.py        # Stage 1: screener.in IPO scraper
|   |-- fetch_stock_data.py      # Stage 2: yfinance data fetcher
|   |-- fetch_screener_data.py   # Stage 2b: screener.in financial enrichment
|   |-- personas.py              # 10 investor persona definitions + JSON schema
|   |-- analyze.py               # Stage 3: Claude CLI persona analysis engine
|   |-- score.py                 # Stage 3b: composite score aggregation
|   |-- utils.py                 # Shared utilities (logging, atomic JSON I/O)
|
|-- data/
|   |-- ipo_list.json            # Master IPO registry (223 stocks)
|   |-- scores.json              # Pre-computed composite scores for dashboard
|   |-- stocks/
|   |   |-- {SYMBOL}.json        # Per-stock financial data (yfinance + screener)
|   |-- analyses/
|       |-- {SYMBOL}/
|           |-- {persona}.json   # Per-stock per-persona AI analysis
```

---

## Key Design Decisions

### Idempotency Throughout

Every stage checks for existing output before doing work. Re-running the pipeline is always safe -- it picks up where it left off. The `--force` flag is required to overwrite existing data. Failed fetches save error stubs to prevent infinite re-querying.

### Atomic Writes

All JSON files are written via `tempfile.mkstemp` + `os.replace`, ensuring that a crash mid-write never corrupts existing data. This is critical for a pipeline that may run for hours across thousands of analysis calls.

### Multi-Source Data Enrichment

yfinance provides strong data for NSE-listed stocks but often has gaps for recent IPOs and BSE-only SME stocks. The pipeline addresses this by layering screener.in data on top -- filling in P/E, ROE, ROCE, book value, market cap, and business descriptions where yfinance falls short. Data quality scores are recalculated after each enrichment pass.

### Structured JSON Schema Output

Claude's `--json-schema` flag guarantees every analysis conforms to a fixed schema: integer score, enum recommendation, typed arrays for strengths/risks/flags, and a nested metrics object with constrained enum values. This eliminates parsing ambiguity and makes the output directly consumable by the dashboard without post-processing.

### Flat-File JSON as Cache Layer

The data layer is intentionally simple: JSON files on disk, organized by convention (`data/stocks/{SYMBOL}.json`, `data/analyses/{SYMBOL}/{persona}.json`). No database setup, no migrations, no connection strings. The tradeoff is acceptable because the data is write-once-read-many and the dataset fits comfortably in memory.

### Separation of Compute and Presentation

The dashboard (`app.py`) makes zero API calls. All analysis, scoring, and data fetching happens in the pipeline stages. The dashboard reads pre-computed JSON and renders it. This means the dashboard loads instantly and can be deployed as a static data app.

---

## License

MIT
