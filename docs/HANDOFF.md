# HANDOFF — WisdomInvest living engine (session state, updated 2026-07-19)

## MIGRATING TO A NEW SYSTEM — fully dockerized, 3 steps

1. `git clone https://github.com/thronecloud/ipo.git && cd ipo && git checkout living-engine`
   (main is stale; everything lives on living-engine).
2. **Copy `backups/ipo_migration_20260719.dump`** (243 MB, NOT in git) into `backups/`.
   All data — 2,657 stocks, 6.5M bars, 5,567 analyses — is in it. A copy is parked on
   the owner's Hetzner box (root@46.62.236.46, key `~/.ssh/pclaw2026hertzner`):
   ```
   scp root@46.62.236.46:/root/ipo-transfer/ipo_migration_20260719.dump backups/
   ```
   (sha256 recorded in the transfer session; verify after download. NOTE: the
   `hetzner-canton` ssh-config entry points at a stale rebuilt host — the live box
   is 46.62.236.46.)
3. `docker compose up -d --build` — that's it. The db container **auto-restores the
   newest `backups/ipo_*.dump` on first boot** (scripts/db-init/, verified end-to-end
   2026-07-19: fresh volume → full restore incl. alembic head 53de03c90075). Web :3000,
   API :8000. The api may restart once or twice while the restore runs — self-heals.

No host Python needed:
- **Tests in-container**: `docker compose --profile test run --rm test` → 189 passed
  (verified). Host venv (`python3.10 -m venv .venv; pip install -r requirements.txt`)
  remains optional for dev ergonomics.
- **Analysis in-container**: engine image ships Node + Claude Code CLI. Set
  `CLAUDE_CODE_OAUTH_TOKEN` in `.env` (`claude setup-token` anywhere, token is portable)
  and the scheduler's hourly analyze batches go live. Ad-hoc runs:
  `docker compose exec scheduler python -m engine.run analyze --universe ipo_2026 --workers 10`.
- Optional `.env` (compose has defaults): ANALYSIS_MODEL=claude-fable-5,
  ANALYSIS_BACKEND=cli, ADMIN_TOKEN, CLAUDE_CODE_OAUTH_TOKEN. For host-side venv work
  add DATABASE_URL=postgresql+psycopg://ipo:ipo@localhost:5432/ipo.
- Ongoing backups: the backup container dumps daily into ./backups (14-day retention),
  so any machine with a recent clone + backups/ folder can resurrect the whole system.

## 2026-07-06→19 additions (all committed)

- **Web auto-refresh**: Discovery/TopBar silently refetch every 60s while visible
  (stale-tab fix — `useAsync(..., {refreshMs})`).
- **Owner research artifacts**: Google Sheet "IPO 2026 Analysis Board — FINAL (109/109)"
  (in priyeshu1@gmail.com Drive); SEDEMAC Q4FY26 deck reviewed — corrigendum restated
  customer concentration 49%→58% (single customer likely ⅔ of revenue); thesis: best
  business in vintage, wrong price at ~120x; accumulate on derating; checkpoints =
  Q1FY27 ISG launches, MF3 plant Q2FY27, annual-report concentration disclosure.
- **Top-5 thematic picks delivered** (grid/electrification capex = dominant tailwind):
  AVANA, SEDEMAC (on derating), VIVIDEL, OMPOWER (watch OCF), TIPCO. AI: only FRACTAL
  is institutional-scale, services economics. CLEANMAX: real tailwind, levered equity.
- Fresh migration dump: `backups/ipo_migration_20260719.dump` (2026-07-19).

## 2026-07-06: ipo_2026 universe COMPLETE (109/109, single-model councils)

- All 109 ipo_2026 stocks fully analyzed (10 personas each), one-company-one-model:
  ~96 Fable (prompt v4), 13+ Opus 4.8 (`analyze --symbols ... --model opus --workers 10`).
  Zero BUYs; ~24 HOLDs led by VALUE360/ADISOFT/Msafe (LCB ~46-47); rest AVOID.
- Analysis engine now: `--workers N` parallel personas (10× throughput),
  `--symbols` on analyze CLI, recent-IPO prompt context (v4), verbatim CLI error
  in dead-letter (usage-limit failures purged, never dead-letter transient quota).
- find_work accepts screener-full snapshots (pinned by test) — fresh listings
  analyzable before Yahoo covers them. Screener classification → sector/industry
  gapfill; API mcap/price falls back to screener ratios (108/109 sectors filled).
- Owner deliverable: Google Sheet "IPO 2026 Analysis Board — FINAL (109/109)"
  (formulas: ARRAYFORMULA returns/days-listed/upside + summary block).
- Max-plan usage windows cap ~300-360 CLI pairs; probe quota before big runs
  (see scratchpad run_split_analysis.py pattern). 189 tests green.

## NEW since the morning handoff (all committed on living-engine, 173 tests green)

1. **P4 hygiene committed** (f2905b1): analyses natural-key dedup + upsert, analysis
   dead-letter (`analysis_failures`), gapfill loop-closure (quarters/stale_prices
   routing, isin/cap_category identity fill, post-fill re-audit), silent excepts
   killed, DATABASE_URL fail-loud, legacy src/score.py retired.
2. **Benchmark layer** (088e2c9): `index_prices` table (migration 53de03c90075,
   applied to prod) + `engine/ingest/index_prices.py`. **Yahoo reality: ^CNXSC serves
   1 bar (dead); BSE-SMLCAP.BO frozen since 2024-05-30; primary comparator is
   ^CRSLDX (Nifty 500, 2005→today), plus ^NSEI.** 10,234 bars in prod. Daily
   scheduler job `index_prices` at 12:00 UTC.
3. **Backtest engine** (e68f18f): `engine/backtest/study.py` point-in-time event
   study — entry = first close STRICTLY after information_date; survivorship-safe;
   excess vs benchmark same-window; groupings by tier/LCB-quintile/composite-quintile/
   recommendation; Spearman ICs. `python -m engine.run backtest` CLI +
   `GET /api/backtest`.
4. **LEAK#1 API** (19c3f22): /api/stocks exposes lcb + confidence_tier, sort=lcb;
   detail composite exposes lcb/tier/stderr/axis_scores/factor_version.
5. **Ops**: 23 orphaned 'running' job_runs marked error; api+scheduler containers
   rebuilt on current code. Scheduler has NO Claude credential
   (analyze available=False) — set CLAUDE_CODE_OAUTH_TOKEN to drain backlog (P5).
6. **Price backfill truth**: 146/512 scored names have NO yfinance history at all
   (BSE-SME) — hard coverage boundary; backtest reports them as unpriced, never drops.

**Pilot event-study results (1 cohort, ~2.5mo forward, NOT judgment day):**
overall +2.5% mean excess @21d (could be smallcap beta vs Nifty 500);
high tier beats others (+3.95% @21d, 60% hit) but Spearman IC slightly NEGATIVE
(composite −0.097, lcb −0.094 @21d) — score LEVEL not yet predictive, tier maybe;
AVOID names outperformed BUY in the rally. K_DISP/K_COV still uncalibrated priors.
63d/126d horizons unlock as history accrues (first cohort info-date 2026-04-20).

**In flight at handoff time:** web UI for backtest page (/admin/backtest) + LEAK#1
confidence surfacing (Research Desk lcb ranking, tier chips, honesty copy) — check
`git status` in web/; if uncommitted changes exist, review + `cd web && npm run
build` + commit; then `docker compose build web && docker compose up -d web`.

---

# Below: morning handoff (superseded where the above says so)

**Read this first after a context clear.** It is the single source of truth for where the
project stands and what to do next. Product frame: **private prop-trading tool for the
owner. No buyers, no monetization, no SEBI/compliance work, personalization welcome, no
approval gates — proceed autonomously.**

## Where things stand

- Branch `living-engine` (main untouched). **128 tests green** (`pytest tests/`, uses
  throwaway `ipo_test` DB). Migrations linear, head applied to prod DB `ipo`;
  `tests/test_migrations.py` guards ORM⇔Alembic parity.
- Prod stack: docker compose (db/api/web/scheduler/backup) all healthy; web at :3000,
  api at :8000. Scheduler jobs: upcoming, discover, refresh, **refresh_stuck (Sun 06:00)**,
  enrich, amfi, analyze, **score (:50 hourly reconcile)**, dq_audit (05:00), dq_fill (Sat).
- Analysis backend: `claude -p --model claude-fable-5` CLI (Max plan). 512/2,643 stocks
  scored; book is bearish (mostly AVOID; ~1 honest BUY).

## Certified work DONE this session (all manager-certified, red→green TDD)

1. **P0** — composite `UNIQUE(stock_id)` + upsert-under-lock + `reconcile_scores`;
   LLM output contract enforced in `save_analysis` (`engine/analysis/contract.py`);
   coverage-correct composite = mean(persona 0-10)×10 (0-100), clamped, derived
   TOTAL_PERSONAS. Old-formula composites backfilled (F1).
2. **Determinism** — `id.desc()` tiebreak on every "latest" query; migration-parity test.
3. **Tier 2.5** — `composite_score_history` append-only point-in-time table
   (`information_date = max(analyzed_at)` is the backtest key). First cohort captured
   (512 rows, info date 2026-04-20). Written on every recompute, idempotent per day.
4. **LEAK#1 confidence layer (backend)** — `engine/scoring/confidence.py`, frozen
   partition `axis-v1`: core={buffett,munger,damani,jhunjhunwala}, growth={fisher,lynch},
   value={graham,marks,greenblatt}, independent={kedia}. Pole-median axis scores; tiers
   {high, moderate, provisional (axis missing), mixed (poles diverge)};
   `LCB = composite − 1·stderr_eff(axes) − 3·(4−axes_present)`. Persisted on
   `composite_scores` + history (`axis_scores`, `confidence_tier`, `score_stderr_eff`,
   `lcb`, `factor_version`). Prod backfilled: high 243 / moderate 100 / provisional 110 /
   mixed 59. **Rationale (R2 study): the 10 personas are ~2.3 independent voices
   (PC1=64%); never market "10 experts agree"; stdev-of-10 confidence is theater.**
5. **P3 hardening (all)** — ON CONFLICT conflict-safe writes (`add_snapshot`,
   `upsert_daily_prices`, `get_or_create_stock`); atomic `add_universe_tag(session,…)`
   (DB-side JSONB append; signature changed — callers pass session); deterministic
   `as_of` audit + insert-only-on-change (`audit()` idempotent); `Stock.last_fetched_at`
   (attempt-tracking freshness); stuck-status rework in `yf_refresh.refresh`:
   new stocks get grace (park only after `PARK_THRESHOLD=3`), viability = latest yf
   snapshot `data_quality=="full"` (minimal data no longer promotes/resets), atomic
   `bump_fetch_failures` RETURNING, auto-revive stale/unfetchable on success, weekly
   `refresh_stuck` sweep.
6. **P4-M3** — fundamentals-only content hash (`VOLATILE_INFO_KEYS` + drop
   `history_summary` from hash): price/volume moves no longer churn snapshots or
   re-trigger analysis.

**NOTE:** the final P3 + M3 packages were implemented and are green but the manager
agent's certification reply was cut off by an API error — treat them as "done, cert
pending" (a fresh review pass is fine).

## Key research findings (decision-grade, all in repo history)

- **R1 (backtest audit):** score had no time dimension → fixed via history table. Only
  ~1 month forward price data from the Apr-2026 cohort; **no benchmark index in DB**;
  147/512 scored stocks unpriced (mostly BSE-SME). True backtest needs months of accrued
  history; near-term only a 1-month pilot event-study is honest.
- **R2 (persona validity):** mean pairwise r=0.586; PC1=64%; effective voices ≈2.3.
  Basis of the axis-v1 confidence design.
- **Munger persona review:** single-voice BUY was "a printed lie" (fixed by LEAK#1);
  macro ±40% multiplier = "two unknowns multiplied" → **display-only, never in rank
  until backtest evidence**; the real moat is the DQ/data engine, not the persona oracle.
- **DQ system** (built earlier, live): `engine/quality/` audit+gapfill, A-F grades,
  nightly; avg quality 82.8; dashboard `/admin/data-quality`.

## PENDING queue (execute top-down; manager pattern: red→green test per package)

1. **P4 remainder (hygiene):**
   - X1: unique/dedupe `analyses` natural key (stock_id, persona, data_hash, model) +
     dedup migration.
   - Gapfill loop-closure: route `quarters` + `stale_prices` missing-tokens; extend
     `_fill_identity` to isin/cap_category; post-fill targeted re-audit read-back.
   - Analysis retry/dead-letter: failed (stock,persona) pairs persist a marker so
     `find_work` skips them (stop re-burning the daily cap).
   - Kill silent `except Exception: pass` (repo.py, scheduler.py, yf_refresh.py — log
     ≥WARN); JobRun with high item-error ratio must not report clean "success".
   - `DATABASE_URL` fail-loud (db/base.py silently defaults to localhost).
   - Retire legacy `src/score.py` SUM-scorer (contradicts DB mean×10).
2. **P2 — BACKTEST (north star):** close price gap for scored names (backfill_prices);
   ingest Nifty Smallcap benchmark (`^CNXSC`) into an `index_prices` table;
   survivorship-safe pilot event-study: forward returns by confidence_tier and LCB
   quintile vs benchmark, from `composite_score_history` × `daily_prices`, cohorts
   frozen on `information_date`, **never filter status='active', never delete parked
   names**. Answers: does high-tier/high-LCB outperform? does LCB beat raw composite?
   Coefficients (K_DISP=1, K_COV=3, MAG=15) are unvalidated priors to calibrate.
3. **LEAK#1 UI:** Discovery ranks by `lcb`; expose/show `confidence_tier` chips,
   "~2-3 independent signals" honesty copy, provisional/mixed badges. API: consumer.py
   list+detail add the new fields.
4. **Macro Board (display-only):** research pipeline per certified 5-WP plan in history —
   sector×(industry) themes, daily 14:15 UTC job, Δ-cap 0.15/day, clamp 0.6-1.4;
   **never multiplies rank** unless backtest proves it adds edge.
5. **P7:** event-driven re-analysis (price-shock/earnings triggers → queue jump) +
   personal alerts (personalization fine now).
6. **P5:** analyze the dark 81% backlog (wire ApiBackend or push CLI cap), triage by
   owner interest.
7. **CAPSTONE:** walk all 10 personas (their real `src/personas.py` prompts) through the
   finished product, collate top-10 asks, implement.

## Conventions to preserve

- TDD: failing test first; every schema change ships ORM + Alembic migration together.
- Determinism invariants: frozen `factor_version`; no live population fitting in scoring;
  every "latest" query has `id.desc()` tiebreak; hashes exclude volatile fields.
- Survivorship invariant: no hard-delete of parked stocks; no `status='active'` filters
  in historical/backtest queries.
- Composite point estimate (mean×10) is immutable; confidence/macro are additive layers.
- Env: `source .venv/bin/activate`; prod DB `postgresql+psycopg://ipo:ipo@localhost:5432/ipo`;
  tests set their own `ipo_test` URL via conftest.
- Optional pattern from this session: a "Principal Engineer manager" subagent certifying
  each package against red→green bars — respawn one if useful (it does not survive
  context clears; this file replaces its memory).
