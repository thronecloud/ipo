# HANDOFF — data-integrity remediation (2026-07-20)

**Read this with `docs/superpowers/plans/2026-07-20-data-integrity.md` (the plan) and
`.superpowers/sdd/progress.md` (the per-task ledger, gitignored but on disk).**

Branch `living-engine`, at `17f8007`. Tests **189 → 246**.
Production intact throughout: 2,657 stocks / 5,567 analyses / 6,495,088 bars.

Execution method: superpowers subagent-driven-development — one implementer per task,
an independent reviewer after each, fix rounds until both spec and quality verdicts pass.

---

## RESUME HERE

**1. The Task 9 follow-ups are settled** — landed in commit `17f8007`. The corrective
upsert now returns inserted-only rows, tallies `bars_rebased` into job stats from refresh,
index refresh **and** backfill, dedupes dup-date frames on the conflict key, and never lets
an incoming NULL degrade a stored bar (asymmetric change predicate + per-field coalesce).
The two previously failing tests pass. Tests **231 → 246**. No stash remains.

**2. Next planned task is Task 10** (field provenance — `sector_source`/`isin_source`, so
imputed values stop posing as measured), from
`docs/superpowers/plans/2026-07-20-data-integrity.md`. Phase 2 tasks 10–12 remain, then
Phases 3–4.

---

## Owner action items (nobody else can do these)

- **`CLAUDE_CODE_OAUTH_TOKEN` is unset**, so `analyze: available=False`. The analysis
  backlog (2,085 of 2,657 stocks unscored) cannot drain until `claude setup-token`
  output goes into `.env`. **Hold this until Phase 3 lands** — see "Do not re-analyse yet".
- ntfy topic is live and subscribed. Alerting works.

---

## What shipped

**Phase 0 — durability (tasks 1-4).** Backups had **never once run** on this machine;
the container raced Postgres on every restart and slept 24h after losing. Now: readiness
gate per cycle, atomic temp→verify→promote, retention that cannot touch
`ipo_migration_*.dump` or `manual_*.dump`. Restore now fails loudly instead of silently
producing an empty database that looks healthy. Postgres moved to loopback. Alerting
wired. Orphan job_run reaper (startup + hourly); it cleared the 15-day zombie on deploy.

**Phase 1 — unit correctness (tasks 5-7).** ROE and revenue growth were displayed 100×
too small, D/E 100× too large. One canonical boundary (`api/units.py`), applied at both
API surfaces via a shared `_quote_fields` helper. IEX now reads ROE 39.4% (was 0.4%),
D/E 0.01x. **593 snapshots that rendered above 100× leverage → 0.**

**Phase 2 partial (tasks 8-9).** Prompts now quote the latest close from `daily_prices`
with an optional `as_of` bound, and **every price-derived figure is restated coherently**
(mcap, P/E trailing+forward, P/B, P/S, dividend yield inverse, EV *equity leg only* with
net debt held constant, EV/EBITDA, EV/Revenue, `ipo_return_pct`). Dividend yield was also
double-scaled — 2,050 of 5,567 analyses rendered it 100× high. `upsert_daily_prices` now
corrects provider-rebased bars instead of ignoring them, plus a cliff detector.

---

## Things a future session must not get wrong

- **`src/` is LIVE**, not legacy, despite what the old `docs/HANDOFF.md` implies.
  `src/personas.py`, `src/analyze.py`, `src/fetch_*.py` are imported by `engine/`. Only
  `src/utils.py` is unreferenced. Deleting `src/` breaks production.
- **Never `docker compose down -v`** — destroys `ipo_pgdata`.
- **Do not re-analyse yet.** Task 13 (transient-vs-permanent dead-letter classification)
  is unbuilt, so a quota outage still permanently drops stocks from coverage. And Task 15
  will tighten the contract, which without Task 13 converts stricter validation into
  silent coverage loss. **Order matters: 13 before 15, both before any re-run.**
- **`PROMPT_VERSION` is still `v4`** though the prompt's content materially changed in
  task 8. Old and new prompt outputs are indistinguishable in the corpus. A reviewer
  suggested stamping a template hash / `prompt_variant` so they separate without
  triggering re-analysis of all 5,567.

---

## Findings that changed what we believe

**The negative IC is not a verdict.** Measured live: IC(21d) = −0.0973, n=354, bootstrap
95% CI **[−0.199, +0.008]** — crosses zero. Every cohort member shares one April 2026
window, so the study has one time-series observation. The honest reading is "no
measurable signal yet, in either direction." **Do not calibrate `K_DISP`/`K_COV` against
it** and do not conclude the model is dead.

**Model choice, not the model, may explain it.** `opus`-dominant stocks average composite
**24.1**; `sonnet`-dominant **33.9**. Population SD ~11. Which model scored a stock moves
it ~0.9 SD for reasons unrelated to the business. 86 stocks (15% of the book) sit at the
bottom of every ranking as an artifact. Phase 4 re-runs them on one model — 860 analyses,
~3 quota windows. This is the highest-value action available.

**771 price cliffs across 325 stocks (12.2%) — but blast radius is zero today.** They are
demergers Yahoo never records (verified live on ABFRL, QUESS — a demerger never enters the
`splits` array, so `auto_adjust` structurally cannot fix it). Not our bug. All cliffs are
≤ 2026-03-11; the cohort's earliest entry is 2026-04-20. **Published backtest numbers are
not wrong from these.** But ~2-3 new ones land per month, and as horizons extend to 63 and
126 days one will eventually fall inside a window.

**`^CRSLDX` benchmark is 9 days stale** (ends 2026-07-08, prices run to 2026-07-17), so
`excess_to_date` compares mismatched windows — a real, live distortion of order 1-2% in a
number the dashboard publishes. Unfixed, not yet scoped to a task.

**Single-persona contamination.** 72 composites sit at coverage 1; `confidence.py:89`
hardcodes `stderr_eff = 0.0` at n=1, so one opinion earns *zero* uncertainty and LCB is
the default Discovery sort. `CompositeScoreHistory` rows are written after every
individual persona success and the backtest takes the earliest via `DISTINCT ON`, so
**13% of the cohort is single-persona and never self-heals.** Task 14 fixes this.

**The confidence tier is anti-correlated with quality.** `abs(composite − 50) ≥ 15` is
direction-blind on a population centred at 32.7, so `high` tier has mean composite **25.6**
(lowest) and `mixed` has **46.2** (highest) — and `TierChip` renders `high` in green.
Task 14 fixes this.

**`consensus_recommendation` ignores the council.** It is a pure composite threshold, so
ten personas each returning HOLD/6 yields composite 60 → displayed **"BUY"** with
`{"HOLD": 10}` beside it. Nothing in the prompt stack ever defines BUY/HOLD/AVOID.
Task 15 fixes this.

**The 189 tests earned high confidence that the pipeline moves data correctly and
near-zero that the numbers mean what they claim.** Real Postgres integration tests, no
mock theater — but `K_DISP` could be set to `0.0` or `1000.0` with everything green, and
`tests/test_analysis_parallel.py:320` *asserts* that a rate-limit error dead-letters all
ten personas, certifying the Task 13 bug as correct behaviour.

---

## Deferred minor findings

Full list in `.superpowers/sdd/progress.md` (items B–S). The ones most worth picking up:

- `_quote_fields` single-sources *conversion* but not *extraction* — detail hand-builds
  its raw dict, list passes `row._mapping`. Add a field to one and the `3PLAND`
  divergence class returns.
- `as_of` in `prompt.py` has no caller yet; Phase 4's historical re-run must wire it.
- Negative EV renders unformatted via `src/analyze.py` `format_currency`.
- `SCHED_REAP_ORPHAN_HOURS` unreachable from `.env` (scheduler has no `env_file:`).
- `DEPLOY.md` documents macOS on a Linux host; `docs/HANDOFF.md` publishes the Hetzner IP and
  ssh key filename — check repo visibility.
