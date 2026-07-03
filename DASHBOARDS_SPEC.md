# WisdomInvest Dashboards — Build Spec

Shared contract for two parallel builds: the **API** (`api/`) and the **web app** (`web/`).
Both must agree on the API shapes below. Design direction is **"The Terminal & The Council."**

## Architecture
```
Postgres (existing, via DATABASE_URL in .env)  ← the living engine's store, READ-ONLY here
        │
   api/  FastAPI read-layer (Python, reuses db/ SQLAlchemy models)   :8000
        │  JSON over HTTP, CORS allow http://localhost:3000
   web/  Next.js 14 App Router + TypeScript + Tailwind                :3000
        ├─ Consumer "Research Desk"  (/, /stock/[symbol])
        └─ Admin "Engine Room"       (/admin, /admin/jobs, /admin/coverage, /admin/personas)
```
Run: `uvicorn api.main:app --reload --port 8000` and `cd web && npm run dev`.
Web reads `NEXT_PUBLIC_API_BASE` (default `http://localhost:8000`).

## DB schema (read-only source of truth — see db/models.py)
- **stocks**(id, symbol, isin, company_name, cap_category[small/mid/large], exchange, sector, industry, listing_date, issue_price, ipo_mcap_cr, screener_url, universe[JSON list e.g. ipo_2025/ipo_2026/ipo_2024/nse_smallcap/smallcap_250/microcap_250], status[active/new/unfetchable], first_seen, last_updated)
- **stock_snapshots**(id, stock_id, captured_at, source["yfinance"|"screener"], data_quality[full/partial/limited/minimal], content_hash, info[JSON yfinance .info], financials, quarterly_financials, balance_sheet, cashflow, history_summary, screener[JSON: ratios, profit_loss, quarterly_results, balance_sheet, cash_flow, shareholding, about], current_price, market_cap, pe_ratio, roe, debt_to_equity, revenue_growth) — append-only time series; use latest per source.
- **analyses**(id, stock_id, persona[slug], model, prompt_version, data_hash, score[0-10], recommendation[BUY/HOLD/AVOID], investment_thesis, key_strengths[JSON], key_risks[JSON], red_flags[JSON], detailed_analysis, metrics_evaluated[JSON], analyzed_at, duration_ms, total_cost_usd, usage[JSON]) — latest per (stock,persona).
- **composite_scores**(id, stock_id, computed_at, composite_score[0-100], persona_scores[JSON {slug:score}], consensus_recommendation, recommendation_counts[JSON], analysis_coverage, total_personas)
- **job_runs**(id, job_type, target, status[running/success/error], started_at, finished_at, stats[JSON], error)
- Persona metadata lives in `src/personas.py` (PERSONAS dict: slug, display_name, nationality, system_prompt). API exposes slug+display_name+nationality+a short philosophy line (first sentence of system_prompt is fine).

**Reality to honor:** only ~463 of 2,578 stocks are analyzed so far (Fable runs are gated by Max limits). The API MUST return unanalyzed stocks too, with `composite_score: null` and empty per-persona verdicts — the UI shows them as "Not yet analyzed" / pending conviction strip. Never hide the unanalyzed majority.

## API contract (all GET unless noted; JSON)
**Consumer**
- `GET /api/meta` → `{stocks_total, analyzed, universes:{tag:count}, sectors:[...], last_job_at, personas:[{slug,display_name,nationality,philosophy}]}`
- `GET /api/stocks?universe=&sector=&cap=&consensus=&q=&min_score=&max_score=&analyzed_only=&sort=&order=&page=&page_size=` →
  `{total, page, page_size, items:[{symbol, company_name, sector, cap_category, universe:[...], status, composite_score|null, consensus_recommendation|null, analysis_coverage, per_persona:{slug:{score,recommendation}}|{}, current_price, market_cap_cr, pe_ratio, roe, debt_to_equity, revenue_growth, ipo_return_pct, data_quality}]}`
  Sort keys: composite_score(default desc), symbol, market_cap_cr, pe_ratio, revenue_growth, or a persona slug.
- `GET /api/stocks/{symbol}` → `{identity:{symbol,company_name,isin,sector,industry,exchange,cap_category,universe,listing_date,status}, quote:{current_price,market_cap_cr,pe_ratio,roe,debt_to_equity,revenue_growth,ipo_return_pct,data_quality,as_of}, composite:{composite_score|null,consensus_recommendation|null,recommendation_counts,analysis_coverage,total_personas}, council:[{persona,display_name,nationality,score,recommendation,investment_thesis,key_strengths,key_risks,red_flags,detailed_analysis,metrics_evaluated,model,analyzed_at}], fundamentals:{annual:{metric:{period:value}}, quarterly:{...}, ratios:{...}, shareholding:{Promoters:{period:val},FIIs:{...},DIIs:{...}}, about}, conviction_history:[{computed_at,composite_score,consensus_recommendation}]}`
  (fundamentals come from the latest source="screener" snapshot's `screener` JSON, falling back to yfinance financials; conviction_history from composite_scores rows over time.)

**Admin**
- `GET /api/admin/overview` → `{stocks_total, by_status:{...}, snapshots:{yfinance,screener}, analyses_total, scored, analysis_backlog, coverage:[{universe, tracked, full_data, analyzed}], freshness:{fresh_24h, stale_7d, older}, recent_jobs:[...5]}`
- `GET /api/admin/jobs?limit=&type=` → `[{id,job_type,target,status,started_at,finished_at,duration_s,stats,error}]`
- `GET /api/admin/coverage` → per-universe `[{universe, tracked, full_data, analyzed, backlog}]`
- `GET /api/admin/personas/distribution` → `[{persona,display_name, count, avg_score, score_hist:[c0..c10], rec_counts:{BUY,HOLD,AVOID}}]`
- `GET /api/admin/usage` → `{by_model:{model:{analyses,total_cost_usd,input_tokens,output_tokens}}, total_analyses, backlog}`
- `POST /api/admin/jobs/run` body `{job:"discover|refresh|enrich|analyze|score", args:{}}` → launches `python -m engine.run <job>` as a subprocess (non-blocking), returns `{launched:true, job}`. Guard behind a simple header token `X-Admin-Token` (read ADMIN_TOKEN from env; if unset, allow localhost only). Do NOT run analyze with large batches by default.

## Design system — "The Terminal & The Council"
Cool-ink dark, warm-paper text; brass accent; mono-first numerics; editorial serif for investor wisdom.
- **Color tokens** (Tailwind theme): `ink #0D1014`, `panel #161A1F`, `panel2 #1D222A`, `hairline #262C34`, `paper #EDE8DC`, `muted #8A93A0`, `brass #C8A24B` (brand/HOLD), `sage #4FB286` (BUY), `terracotta #D9614C` (AVOID). Radius 2–4px. Hairline 1px borders.
- **Type** (next/font/google): `IBM_Plex_Mono` (all numerics, tickers, tables, data — tabular-nums), `IBM_Plex_Sans` (UI/body), `Newsreader` (italic-capable serif for persona quotes, thesis text, editorial headers).
- **Signature — `<ConvictionStrip>`**: 10 cells in persona order (Buffett, Munger, Graham, Lynch, Fisher, Greenblatt, Marks, Jhunjhunwala, Damani, Kedia). Each cell colored by that persona's rec: BUY=sage, HOLD=brass, AVOID=terracotta, none/unanalyzed=hairline. Props: `perPersona`, `size` (`xs` for table rows ~10px cells, `lg` for hero ~28px). Hover tooltip: persona name + score. It is the product's visual identity — use it everywhere a stock appears.
- Avoid AI-slop: no purple gradients, no Inter/Roboto, no generic card-with-big-number hero. Dense, tabular, hairline-ruled, keyboard-friendly. Quiet everywhere except the Conviction Strip.

## Consumer "Research Desk" (web/app/(consumer))
- **/ Discovery**: top bar (WisdomInvest wordmark in Newsreader, coverage stat "463 of 2,578 analyzed"), left filter rail (universe, sector, cap_category, consensus, score range, analyzed-only, search), main = dense ranked table: rank, ConvictionStrip(xs), Symbol(mono, links), Company(serif), Composite (mono + thin bar), Consensus chip, Sector, MCap, P/E, Rev growth. Sortable headers, pagination. Persona-weighting control (select which of the 10 count → recompute composite client-side, like the current Streamlit persona selector). Persist selections + watchlist in localStorage.
- **/stock/[symbol] Stock detail**:
  - Hero: company (serif), ticker+sector+exchange (mono), price/mcap/PE, big composite + `<ConvictionStrip size=lg>`, consensus, data-quality/confidence badge.
  - **The Council**: 10 persona blocks (in fixed order) — each: portrait/monogram, name (serif), nationality tag, score/10 (mono, color), recommendation chip, investment_thesis (serif italic), strengths/risks two-col, red flags, expandable detailed_analysis. Unanalyzed → "Awaiting the council."
  - **The Debate**: synthesized bulls vs bears — group personas by BUY vs AVOID, surface the shared strengths (agreement) and shared risks/red-flags (disagreement). Derive client- or server-side from the council array.
  - **Fundamentals**: multi-year P&L small-multiples (Sales, Net Profit, OPM%, EPS from screener), quarterly, key ratios (ROCE/ROE/D-E/PE), promoter/FII/DII shareholding trend. All numerics mono/tabular.
  - **Conviction over time**: line of composite_score history (may be single point now — handle gracefully).
- Empty/loading/error states for every fetch. Responsive to mobile. Keyboard focus visible.

## Admin "Engine Room" (web/app/(admin))
- **/admin Overview**: health cards (stocks, yfinance/screener snapshots, analyses, scored, backlog), **coverage** by universe (tracked / full-data / analyzed as stacked bars or a heatmap), **freshness** (fresh<24h / stale / old), recent jobs feed, per-persona distribution mini-bars, usage/cost by model. Dense terminal aesthetic.
- **/admin/jobs**: job_runs table (type, target, status color, duration, stats, error) with a control panel of buttons to trigger discover / refresh / enrich / score (analyze only via an explicit small-batch input, since it spends Max limits) → POST /api/admin/jobs/run, then poll.
- **/admin/coverage**: per-universe tracked/full/analyzed/backlog table + bars.
- **/admin/personas**: per-persona score histograms + BUY/HOLD/AVOID split (drift detection).

## Quality floor (both)
Typed API responses; loading/empty/error states; no secrets in client; CORS locked to localhost dev; mobile-responsive; keyboard-accessible; no console errors. Numerics always mono + tabular-nums. Honor the unanalyzed-majority reality everywhere.
