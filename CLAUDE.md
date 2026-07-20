# WisdomInvest — Indian IPO Analyzer

AI-powered fundamental analysis of Indian IPO stocks (NSE/BSE) through 10 legendary
investor personas (Buffett, Munger, Graham, Lynch, Fisher, Greenblatt, Marks,
Jhunjhunwala, Damani, Kedia). It runs as an always-on "living engine": Postgres is the
durable store, a scheduler daemon ingests and analyses continuously, and Next.js
dashboards read a FastAPI layer.

## Tech stack

- **Store:** Postgres 16, schema via Alembic (`db/`, `alembic/`).
- **Engine:** Python — ingestion, analysis, quality, scoring, backtest, scheduler (`engine/`).
- **API:** FastAPI read-layer (`api/`, port 8000).
- **Web:** Next.js / TypeScript dashboards (`web/`, port 3000).
- **Analysis backend:** the Max-plan `claude` CLI (`claude -p --json-schema`), no API key.
- **Data sources:** yfinance (`.NS`/`.BO`), screener.in (HTML scrape), AMFI (universe).
- Everything ships as Docker Compose services.

## How to run

```bash
docker compose up -d --build          # db, api, web, scheduler, backup
docker compose ps                     # all services should be healthy
docker compose logs -f scheduler      # watch the engine work

# Tests (containerized; no host Python or DB needed). Creates/drops its own
# ipo_test_<id> database on the db service.
docker compose --profile test run --rm test

# Manual engine ops (inside the running scheduler container)
docker compose exec scheduler python -m engine.run status
docker compose exec scheduler python -m engine.run discover --year 2026 --pages 6
docker compose exec scheduler python -m engine.run refresh --limit 50
docker compose exec scheduler python -m engine.run analyze --limit 10   # spends Max quota
docker compose exec scheduler python -m engine.run score
```

Migrations are **not** run by hand — the `api` service runs `alembic upgrade head` on
boot before serving. See `DEPLOY.md` for the full runbook (scheduler cadences, backup/
restore, server overlay).

## Project structure

- `engine/` — the living engine:
  - `ingest/` — screener/yfinance/AMFI/index pulls, IPO discovery, enrichment
  - `analysis/` — persona analysis (prompt build, claude backend, output contract)
  - `quality/` — data-quality scoring, gap-fill, audit
  - `scoring/` — composite confidence scoring
  - `backtest/` — signal/IC study
  - `scheduler.py` — always-on cron daemon; `run.py` — CLI entrypoint; `repo.py` — DB access; `notify.py` — ntfy/webhook alerts
- `api/` — FastAPI app (`main.py`), routers (consumer + admin), Pydantic schemas, unit conversion (`units.py`)
- `web/` — Next.js dashboards: Research Desk (`/`) and Engine Room (`/admin`)
- `db/` + `alembic/` — SQLAlchemy models and migrations
- `src/` — **shared library** for the engine (imported in 10+ places): `personas.py`
  (persona defs + prompt template + JSON schema), `fetch_*.py` (screener/yfinance
  scrapers), `analyze.py` (financial-summary + currency formatters), `utils.py`.
  `run_pipeline.py` / `run_analysis.sh` are the legacy gen-1 file-based entrypoints that
  still drive these modules; the scheduler is the live path.
- `scripts/` — backup/restore, db-init, one-off backfills, JSON→DB migration, benchmarks
- `docs/` — plans, specs, research, archived handoffs

## Key conventions

- **`src/` is live**, not legacy — `engine/` and `api/` import from it. Do not delete it.
- **Analysis backend:** `ANALYSIS_BACKEND=cli` shells out to `claude -p
  --no-session-persistence --output-format json --json-schema`. Containers can't read the
  Keychain login, so headless auth is a long-lived `CLAUDE_CODE_OAUTH_TOKEN` (from
  `claude setup-token`) in `.env`. Unset ⇒ scheduler runs data-only and skips analysis.
- **`.env` keys** the stack reads: `CLAUDE_CODE_OAUTH_TOKEN` (analysis auth),
  `NTFY_TOPIC` (failure alerts — must also be *subscribed*, or alerts are silent),
  `NOTIFY_WEBHOOK_URL` (optional second channel), `ADMIN_TOKEN` (guards job-trigger
  endpoints), the `SCHED_*` tuning knobs, and the prod-overlay `SITE_ADDRESS`/`ADMIN_HASH`.
  `.env` is gitignored.
- **Backups are the crown jewels.** The `backup` service dumps nightly to `./backups/`
  (14-day retention; never touches `manual_*`/`ipo_migration_*`). A fresh `db` volume
  auto-restores the newest dump on first boot — that is the machine-migration path.
  **Never `docker compose down -v`** (destroys `ipo_pgdata`). Restore drills:
  `scripts/backup/restore.sh`.
- Idempotent ingestion: re-running is safe; `--force` overrides caches. `claude` analysis
  calls take ~30-100s each (sonnet ~30s, opus/fable slower).

## History

This began as a gen-1 file pipeline: `src/` scripts wrote `data/*.json`, a Streamlit app
read them. That app is retired. **Postgres is now the source of truth.** The `data/*.json`
tree (`ipo_list.json`, `stocks/`, `analyses/`, `scores.json`) is the gen-1 cache, retained
for reference and one-time migration (`scripts/migrate_json_to_db.py`) — not authoritative.
