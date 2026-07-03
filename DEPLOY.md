# WisdomInvest — Production Runbook

The whole living engine runs as five Docker services on this machine.
Postgres data lives in the `ipo_pgdata` volume; dumps land in `./backups/`.

## Deploy / update

```bash
docker compose up -d --build     # build + start everything (db, api, web, scheduler, backup)
docker compose ps                # all services should be healthy
```

- Dashboards: http://localhost:3000 (Research Desk) · http://localhost:3000/admin (Engine Room)
- API: http://localhost:8000/health
- Migrations run automatically (`api` runs `alembic upgrade head` on boot).

## What runs when (scheduler, UTC)

| Job      | Cadence          | What it does                                              |
|----------|------------------|-----------------------------------------------------------|
| discover | daily 14:00      | Append-only: registers newly listed IPOs from screener     |
| refresh  | daily 02:00      | yfinance re-pull, hash-gated (batch 100)                   |
| enrich   | Sunday 03:00     | screener fundamentals batch (batch 50)                     |
| analyze  | hourly at :30    | Persona analysis batch (20), **daily cap 200**, credential-gated |

Watch it: `docker compose logs -f scheduler`
Tune via `.env`: `SCHED_ANALYZE_BATCH`, `SCHED_ANALYZE_DAILY_CAP`, `SCHED_REFRESH_BATCH`, `SCHED_ENRICH_BATCH`.

## Enabling analysis inside Docker (one-time)

The Max-plan CLI login lives in the macOS Keychain, which containers can't read.
Generate a long-lived headless token instead:

```bash
claude setup-token        # interactive, opens browser
```

Put the printed token in `.env` as `CLAUDE_CODE_OAUTH_TOKEN=...`, then
`docker compose up -d` (recreates with the env). Until then the scheduler runs
**data-only autonomy** and logs "analyze SKIPPED — no Claude credential".

## Backups

- `backup` service dumps nightly to `./backups/ipo_<ts>.dump` (keeps newest 14; first dump on boot).
- **Restore drill** (safe, into a scratch DB): `scripts/backup/restore.sh backups/ipo_<ts>.dump`
- **Real recovery**: `scripts/backup/restore.sh backups/ipo_<ts>.dump ipo` (asks for confirmation)
- Consider syncing `./backups/` offsite (iCloud/rclone) — the dumps are the crown jewels.

## Manual operations

```bash
docker compose exec scheduler python -m engine.run status              # engine state
docker compose exec scheduler python -m engine.run discover --year 2026 --pages 6
docker compose exec scheduler python -m engine.run refresh --limit 50
docker compose exec scheduler python -m engine.run analyze --limit 10  # spends Max quota
docker compose exec scheduler python -m engine.run score
```

(or trigger from the Engine Room UI at /admin/jobs — analyze is capped at 25/run there)

## Host requirements (the two manual toggles)

1. **Docker Desktop → Settings → General → "Start Docker Desktop when you sign in"** — required for the stack to survive reboots (all services are `restart: unless-stopped`).
2. Keep the Mac awake: System Settings → Energy → prevent automatic sleeping (or run `caffeinate -s` / use a real always-on host later).

## Local development (unchanged)

The host `.venv` + `uvicorn`/`npm run dev` workflow still works against
`localhost:5432` (the same Postgres container). Stop the containerized
api/web first if you need ports 8000/3000.
