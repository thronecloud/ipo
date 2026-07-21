# Data-integrity deferred-findings triage (2026-07-20)

Lettered minor findings accumulated across tasks 1–9 of the data-integrity remediation
(plan: `2026-07-20-data-integrity.md`). The authoritative running ledger is
`.superpowers/sdd/progress.md` (gitignored, on disk); this file is the tracked snapshot so
the triage survives outside that ledger.

Lettering in the ledger begins at **B** — there is no finding A. Status is **open** unless
noted (deferred at task time, not yet addressed).

| Letter | From | Finding | Status |
|--------|------|---------|--------|
| B | Task 1 | Backup size floor ratchets downward — compares against the newest retained dump, should compare against the largest retained. | open |
| C | Task 1 | `newest_dump_size` ignores operator (`manual_*`) dumps, giving a weak floor on a freshly-seeded machine. | open |
| D | Task 1 | `notify()` is still silent per-call when `NTFY_TOPIC` is unset (only the startup line warns). | open |
| E | Task 2 | A socket-only live Postgres defeats the db-init first-boot guard; needs a config this repo does not produce — note it in the script comment. | open |
| F | Task 2 | If the temp server is down at abort time the guard refuses, so `PG_VERSION` survives and the next boot serves an empty DB as healthy. Fallback: `postmaster.pid` line 6. | open |
| G | Task 2 | Runbook `sha256` on `scp` transfer is still unenforced — the real defence against a truncated dump transfer. | open |
| H | Task 3 | `DEPLOY.md` places `NTFY_TOPIC` inside the prod-overlay-only `.env` block, but base-stack services consume it — misleads a second-box operator. | open |
| I | Task 3 | Credentials are still `ipo:ipo` (loopback shrinks the blast radius but does not fix it). | open |
| J | Task 3 | No test guards the compose wiring against silent deletion. | open |
| K | Task 4 | `SCHED_REAP_ORPHAN_HOURS` is unreachable from `.env` — the scheduler service has no `env_file:`. | open |
| L | Task 6 | `_quote_fields` single-sources *conversion* but not *extraction*: detail hand-builds its raw dict from the ORM, list passes `row._mapping`. A field added to one and not the other reintroduces the `3PLAND` divergence class. | open |
| M | Task 6 | `sort=market_cap_cr` orders by the raw column, so screener-fallback-only stocks sort as NULL despite publishing a value (pre-existing). | open |
| N | Task 7 | 8.8% of stocks render `0.00x`/`0.01x` D/E; none have a true D/E of 0, so every `0.00x` is a rounded-down nonzero. | deferred (judged acceptable precision) |
| O | Task 8 | Negative EV renders unformatted via `src/analyze.py` `format_currency`. | open |
| P | Task 8 | EV multiples are dropped for the 17 already-net-cash stocks. | open |
| Q | Task 8 | `as_of` in `prompt.py` has no caller yet — Phase 4's historical re-run must wire it. | deferred (Phase 4) |
| R | Task 8 | `test_prompt_version_bumped` asserts `v4`; the test name now misleads. | open |
| S | Task 8 | `PROMPT_VERSION` stayed `v4` though task 8 materially changed the prompt — old and new output are indistinguishable in the corpus. Reviewer suggests stamping a `prompt_variant`/template hash. | open |
| T | Task 10 | Prompt-side provenance unaddressed: imputed sector still reaches the LLM prompt unqualified (as if measured). Named in Task 10's Why, absent from its steps — the DB now records provenance but the prompt does not surface it. | open |
| U | Task 10 | `'unknown'` provenance markers are unrecoverable without a re-measure path: gapfill writes a `_source` only when the value is absent, so no path ever upgrades an `'unknown'` (or `'screener'`) marker to `'yfinance'`/`'amfi'`. Clearing the backfilled `'unknown'` requires an active re-measure, which no job performs. | open |
| V | Task 11 | Corpus audit flagged suspected wrong-company mappings pending owner review: **ASMS** (Bartronics vs Avio Smart Market Stack — likely wrong company, analyses may be about the wrong business), **MCL** (Madhav Copper vs M Tek Copper — suspect), **LYPSAGEMS** (Lypsa Gems vs Aurus Gem Corp — suspect), plus renames the new guard will block on refresh: **DPSCLTD** (→ India Power Corp), **MIRCELECTR** (Onida brand alias). **RESOLVED 2026-07-21:** all three suspects were corporate renames, not wrong companies (ASMS=Bartronics→Avio Feb-26, MCL=Madhav→M Tek Jun-26, LYPSAGEMS=Lypsa→Aurus Jul-26 incl. symbol→AURUS). No purges needed. All five stored names corrected in prod; LYPSAGEMS yf_symbol → AURUS.NS. | resolved |
