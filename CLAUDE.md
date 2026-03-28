# Indian IPO Analyzer

AI-powered stock analysis tool that evaluates Indian IPOs through 10 legendary investor personas (Buffett, Munger, Graham, Lynch, Fisher, Greenblatt, Marks, Jhunjhunwala, Damani, Kedia).

## Project Structure

- `src/fetch_ipo_list.py` — Stage 1: Scrape IPO list from screener.in
- `src/fetch_stock_data.py` — Stage 2: Fetch financial data via yfinance
- `src/personas.py` — 10 investor persona definitions + prompts + JSON schema
- `src/analyze.py` — Stage 3: Run Claude CLI persona analysis per stock
- `src/score.py` — Stage 3b: Compute composite scores
- `src/utils.py` — Shared utilities (log, JSON I/O)
- `app.py` — Streamlit dashboard
- `run_pipeline.py` — Pipeline orchestrator
- `data/ipo_list.json` — Master IPO registry (scraped from screener.in)
- `data/stocks/{SYMBOL}.json` — Per-stock yfinance data
- `data/analyses/{SYMBOL}/{persona}.json` — Per-stock per-persona AI analysis
- `data/scores.json` — Pre-computed composite scores for dashboard

## Key Commands

```bash
# Run full pipeline
python run_pipeline.py --stages 1,2,3,4

# Run individual stages
python3 -m src.fetch_ipo_list --year 2025 --pages 25
python3 -m src.fetch_stock_data
python3 -m src.analyze --model opus
python3 -m src.score

# Analyze specific stock
python3 -m src.analyze --symbol ATHERENERG --persona warren_buffett --model sonnet

# Launch dashboard
streamlit run app.py

# Limit stocks for testing
python3 -m src.analyze --limit 5 --model sonnet
```

## Data Pipeline

1. IPO list scraped from screener.in/ipo/recent/ (376 IPOs for 2025)
2. Financial data fetched from yfinance (.NS for NSE, .BO for BSE-only)
3. AI analysis via `claude -p --model opus --json-schema` (Claude Max plan, no API key)
4. Composite scores computed from persona analyses
5. Dashboard reads pre-computed JSON files (no API calls at runtime)

## Important Notes

- JSON files are the cache layer — never re-query data already stored
- Use `--force` flag to re-fetch or re-analyze
- yfinance: BSE-only SME stocks have limited data; NSE stocks have full data
- Claude CLI: uses `--no-session-persistence --output-format json --json-schema`
- Each analysis call takes ~30-100s depending on model (sonnet ~30s, opus ~60-100s)
- Full pipeline for 376 stocks x 10 personas = 3,760 analyses
