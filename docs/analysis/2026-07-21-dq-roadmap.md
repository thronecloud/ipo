# Data-quality roadmap (2026-07-21)

Triage of the open lettered findings from the data-integrity remediation
(`docs/superpowers/plans/2026-07-20-data-integrity-triage.md`, ledger
`.superpowers/sdd/progress.md`) into **do-now / do-later / won't-do**, plus new
data-quality work the triage does not cover. Effort key: **S** ≤ 2h, **M** half-
to full-day, **L** multi-day.

The 17-task remediation closed the structural holes (durability, unit
correctness, provenance stamping, corpus-pollution guards). What remains is a
tail of hardening items plus a class of *cross-source correctness* checks the
remediation never had in scope. Findings V is resolved; N is closed as
won't-do (acceptable precision).

---

## Priority 1 — do now (small, high leverage, unblocks trust)

| # | Finding | Effort | Why now |
|---|---------|--------|---------|
| K | `SCHED_REAP_ORPHAN_HOURS` unreachable — scheduler service has no `env_file:` | S | One compose line. Until fixed the reaper tunable is dead config; an operator who sets it is silently ignored. |
| H | `DEPLOY.md` puts `NTFY_TOPIC` in the prod-overlay-only `.env` block though base services consume it | S | Pure doc fix. Misleads a second-box operator into shipping a stack with all alerting dark. |
| G | Runbook `sha256` on the `scp` dump transfer is unenforced | S | The real defence against a truncated 243 MB transfer restoring a partial DB. Add the checksum step + a `sha256 -c` gate to the runbook and restore.sh. |
| R | `test_prompt_version_bumped` asserts `v4`; name now misleads (prompt is v5) | S | Trivial test correction; a stale assertion is a broken window that hides the next real bump. |
| O | Negative EV renders unformatted via `format_currency` | S | 17 net-cash stocks show a raw number in the analyst-facing prompt/detail. Cosmetic but it reaches the LLM. |
| T | Imputed `sector`/`industry` still reach the **LLM prompt** unqualified | M | The DB records provenance (Task 10) but the prompt presents an imputed sector as if measured. This is a *data-quality-into-analysis contamination*: the persona reasons on a guess it cannot see is a guess. Surface the provenance marker in the prompt (e.g. "Sector: Chemicals *(imputed)*"). Ties directly to the point-in-time honesty principle in deliverable 3. |

## Priority 2 — do later (real, but not blocking; batch into a hardening sprint)

| # | Finding | Effort | Note |
|---|---------|--------|------|
| F | Temp server down at abort → `PG_VERSION` survives → next boot serves an empty DB as "healthy" | M | Genuine durability hole. Add the `postmaster.pid` line-6 fallback so the guard fails safe when the probe can't run. Rare trigger, severe effect. |
| B | Backup size floor ratchets downward (compares vs newest, not largest, retained) | S | A run of small dumps lowers the floor permanently; a later truncated dump passes. Compare against `max(size)` over retained. |
| C | `newest_dump_size` ignores operator (`manual_*`) dumps | S | Weak floor on a freshly-seeded machine. Fold operator dumps into the floor set. |
| L | `_quote_fields` single-sources *conversion* but not *extraction* (detail hand-builds from ORM, list uses `row._mapping`) | M | Reintroduces the `3PLAND` divergence class the moment a field is added to one surface only. Route both surfaces through one extraction function. |
| U | `'unknown'`/`'screener'` provenance markers are unrecoverable — gapfill writes `_source` only on an absent value, so nothing upgrades a marker to measured | M | Add a re-measure pass that revisits `_source IN ('unknown','screener')` rows and promotes them when yfinance/amfi later supplies the field. Without it the provenance grade is permanently pessimistic for backfilled rows. |
| S | Stamp a `prompt_variant`/template hash so materially-different prompts at the same `PROMPT_VERSION` are distinguishable | M | Task 8 changed the prompt under a frozen `v4`; only luck (the Task 15 bump) made v5 output separable. A content hash makes the corpus self-describing and immune to a forgotten bump. |
| P | EV multiples dropped for the 17 already-net-cash stocks | S | They get no EV/EBITDA or EV/Revenue line at all. Emit the equity-only EV with a net-cash annotation rather than suppressing. |
| Q | `as_of` in `prompt.py` has no caller | M | Blocked on the Phase-4 historical re-run wiring; keep parked until that campaign runs on prod. |
| I | Credentials still `ipo:ipo` | M | Loopback binding shrinks the blast radius; rotate to a generated secret on both boxes when convenient. Not urgent while Postgres is `127.0.0.1`-bound. |
| J | No test guards the compose wiring against silent deletion | S | A smoke test asserting each service has its expected `env_file:`/port. Cheap insurance for K/H-class regressions. |
| M | `sort=market_cap_cr` orders by the raw column, NULL-sorting screener-fallback-only stocks | S | Pre-existing; sort by the coalesced published value so those names don't sink to the bottom despite having a value. |

## Priority 3 — won't do (documented, not fixed)

| # | Finding | Disposition |
|---|---------|-------------|
| E | Socket-only live Postgres defeats the db-init first-boot guard | Needs a config this repo does not produce. Note it in the script comment (as the triage says) and move on. |
| N | 8.8% of stocks render `0.00x`/`0.01x` D/E (rounded-down nonzero) | Judged acceptable precision. Revisit only if a threshold ever keys on exactly-zero D/E. |

---

## New data-quality work (not in the remediation's scope)

The remediation hardened *single-source* correctness (units, provenance,
identity). The next tier is **cross-source verification** and **freshness
SLOs** — the failure modes that a single source can't self-detect. These are
ordered by leverage.

### N1 — Price-freshness SLO and completeness gate `[M, do-now-ish]`

The process-regression study surfaced this directly: **181 of 572 scored stocks
(32%) have zero `daily_prices` bars locally, and 35 more have their last bar on
or before their `information_date`** — so a third of the corpus can never enter
the backtest, and the entire priced cohort collapsed to the v1/price-blind era.
W1's full-universe rotation (`price_refresh`, least-recently-priced first) now
exists but has **no SLO and no completeness alarm**. Add:

- A `daily_prices` freshness metric per stock (`max(date)` vs today) and a
  universe-level SLO (e.g. "≥ 95% of `active` stocks priced within 3 trading
  days"), surfaced on the admin dashboard and alerted on breach.
- A **completeness gate**: a stock that is `active` and scored but has zero price
  bars is a data hole, not a healthy row — flag it in the DQ report and feed it
  to `dq_fill` with high priority. A scored stock with no prices is worthless to
  the study.
- Detect the *stale-relative-to-view* case (last bar ≤ information_date) as its
  own flag — it silently voids point-in-time measurement.

### N2 — Cross-source price verification (NSE vs yfinance) `[M]`

yfinance is the sole price source; there is no independent check that its
adjusted closes are right (the Task-9 corporate-action rebases were caught only
*within* yfinance's own revisions). Once W4's NSE scrapers (`engine/ingest/
nse_*.py`) land, cross-check the latest yfinance close against the NSE-published
close for the same symbol/date:

- Store both, flag divergences over a tolerance (say > 2% after adjusting for a
  known corporate action) as a `price_divergence` DQ flag.
- This is the only way to catch a *silently wrong* adjusted series — the class
  that fabricated IEML's +307% "IPO return" before Task 8.
- Deterministic, no LLM. Reuses the hash-gated snapshot pattern.

### N3 — Shareholding-pattern consistency checks `[M]`

screener.in publishes quarterly shareholding (promoter / FII / DII / public). It
is ingested but not validated. Add deterministic invariants:

- The four buckets sum to ~100% (flag when |sum − 100| > 0.5).
- Promoter holding shouldn't jump implausibly quarter-to-quarter (> 20 pts)
  without a corresponding corporate action — a common scrape-misalignment tell.
- A **falling promoter stake** is a material risk signal the personas should see;
  surface it as a structured field, not buried prose. (Feeds deliverable 3's
  context layer too.)

### N4 — Staleness SLOs across every source, not just prices `[M]`

Generalise N1. Each source (screener enrich, AMFI roster, index prices, upcoming
calendar) has an implicit cadence but no explicit SLO or "this source has gone
dark" alarm beyond the Task-12 circuit breakers (which catch *outages*, not
*silent staleness*). Define a per-source freshness budget and a dashboard tile;
alert when `max(fetched_at)` for a source exceeds its budget. AMFI in particular
is monthly and easy to forget.

### N5 — Benchmark-series integrity `[S]`

The backtest's excess-return comparator is a single index series (`^CRSLDX`).
`BSE-SMLCAP.BO` already froze on 2024-05-30 and `^CNXSC` serves one bar — exactly
the silent-death failure mode. Add a freshness assertion on each benchmark symbol
(latest bar within N trading days) so a frozen comparator is caught before it
quietly zeroes every excess-return number.

### N6 — Point-in-time drift audit for identity renames `[S]`

Triage V showed five symbols were corporate renames (ASMS, MCL, LYPSAGEMS,
DPSCLTD, MIRCELECTR). The rename guard (Task 11) now blocks *forward*
mismatches, but there is no periodic audit that stored `company_name` still
matches the source. A monthly read-only sweep of first-token disagreements
(the Task-11 corpus audit, scheduled) would catch the next rename before it
looks like a wrong-company mapping.

---

## Suggested sequencing

1. **This week:** K, H, G, R, O (all S) + N1's completeness flag — clears dead
   config, doc traps, and the biggest measured gap (32% price-less corpus).
2. **Hardening sprint:** F, B, C, L, U, S, T, N4, N5 — durability + provenance +
   freshness SLOs.
3. **After W4 NSE scrapers land:** N2 (cross-source price), N3 (shareholding),
   N6 (rename audit) — the cross-source correctness tier.
4. **Parked on prod campaign:** Q, and the v4→v5 re-run that makes the
   process-regression study meaningful.
