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

## Server deployment (always-on, public dashboard)

`docker-compose.prod.yml` is a production overlay for a real always-on host. It
binds Postgres and the API to `127.0.0.1`, removes the web host port, and adds a
Caddy reverse proxy: public Research Desk, automatic HTTPS, and HTTP basic auth
on the Engine Room (`/admin`) and its API — the app itself only token-guards job
*runs*, not the admin read views, so the lock lives at the proxy.

Local dev never loads this overlay. The server opts in with one line in `.env`:

```
COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml
SITE_ADDRESS=your.host.example        # a hostname (not a bare IP) → enables HTTPS
ADMIN_HASH=<bcrypt, every $ doubled>  # see note below
ADMIN_TOKEN=<random>                   # guards job-trigger endpoints
```

Generate `ADMIN_HASH` and escape it in one step — Compose reads `.env` and
interpolates `$`, so each `$` in the bcrypt hash must be doubled to `$$` or the
hash is silently truncated:

```bash
docker run --rm caddy:2 caddy hash-password --plaintext 'yourpassword' | sed 's/\$/$$/g'
```

`SITE_ADDRESS` must be a DNS name for Let's Encrypt to issue a cert (a bare IP
can't be certified). With no domain, a wildcard-DNS host like `<ip>.sslip.io`
works and gets a real cert. Swapping to your own domain is just editing
`SITE_ADDRESS` and `docker compose up -d`.

Deploy / update on the server:

```bash
git pull
docker compose up -d --build     # COMPOSE_FILE makes this include the prod overlay
```

Resurrect on a fresh always-on host: clone → put the dump in `backups/` → write
`.env` (the four keys above) → `docker compose up -d --build`. The db
auto-restores the newest dump on first boot; Caddy fetches HTTPS automatically.

`.env` and `DEPLOY_SECRETS.txt` are gitignored — secrets never enter the repo.
