# Macro context + news synthesizer — design (2026-07-21)

**Design only. No LLM execution.** The synthesis layer described in §3 is
**permission-gated**: it must not run — not even a single call — without the
owner's explicit approval, because it spends Claude Max quota and, done wrong,
breaks the point-in-time discipline the backtest depends on. Everything in §1–§2
is deterministic and can be built and run without that permission.

The goal (review item 8): give the personas the *context* a human analyst would
have — the rate environment, the sector tape, and stock-specific news — so a
verdict is informed by more than a screener snapshot. The hard constraint: a
verdict must record exactly what context it saw, and a historical re-run must see
only what was knowable then. Context that leaks the future is worse than no
context: it silently inflates every backtest.

---

## Architecture at a glance

```
 deterministic ingestion            immutable storage           permission-gated
 (no LLM)                                                        synthesis (LLM)
 ┌──────────────────────┐        ┌────────────────────┐        ┌──────────────────┐
 │ NSE/BSE announcements │──────▶ │ news_items         │        │ context_digests  │
 │ (W4 nse_*.py)         │        │ (hash-gated,       │──┐     │ (frozen per as_of│
 │ RSS: MC/ET/BS company │        │  published_at)     │  │     │  + context_hash) │
 │ RBI press / policy    │        ├────────────────────┤  ├───▶ │  scope: market   │
 │ MOSPI CPI/IIP/GDP     │──────▶ │ macro_indicators   │  │     │         | stock  │
 │ yfinance USDINR/Brent │        │ (released_at)      │──┘     └────────┬─────────┘
 │ /VIX                  │        └────────────────────┘                 │
 └──────────────────────┘                                                ▼
                                                          persona prompt (attributed,
                                                          point-in-time block) →
                                                          Analysis.context_hash
```

Reuses the living engine's existing disciplines wholesale: `job_run` logging,
Task-12 source circuit breakers, hash-gated append-only rows (no duplicate
storage), and the `data_hash` staleness model — the news/context layer is a
second `data_hash`.

---

## 1. Deterministic ingestion layer (no LLM, build now)

Two row types, both append-only and hash-gated exactly like `stock_snapshots`.
Every ingester runs under `job_run`, honours the circuit breakers, and records
`fetched_at`; nothing here calls an LLM.

### 1a. News / announcements → `news_items`

| Source | Access | Notes |
|--------|--------|-------|
| NSE corporate announcements | W4 scrapers, `engine/ingest/nse_*.py` | Board meetings, results, rating actions, allotments. The primary structured feed; map to `stock_id` by `nse_symbol`. |
| BSE announcements | HTML/JSON endpoint | Covers BSE-only SME names yfinance is thin on. |
| Company RSS (Moneycontrol, ET Markets, Business Standard) | RSS/Atom | Broad but noisy; dedupe hard on canonical URL. |
| screener.in "documents" | already scraped | Partly ingested today; fold into the same table. |

Normalisation contract (deterministic, no interpretation): `source`,
`external_id` (source's own id when present), canonical `url` (unique key),
`stock_id`/`isin` (nullable — market-wide items have neither), `category`
(`announcement | result | rating | corporate_action | rbi | macro | sector`),
`title`, `summary`, `published_at` (**the source's timestamp — the
point-in-time anchor, never `fetched_at`**), `fetched_at`, `content_hash`.
Dedup on `(source, external_id)` then canonical `url`; a re-fetch that yields an
identical `content_hash` is a no-op (same discipline as snapshots).

### 1b. Macro indicators → `macro_indicators`

| Indicator | Source | Cadence |
|-----------|--------|---------|
| Repo rate, policy stance | RBI press-release RSS / DBIE | per MPC (~6/yr) |
| CPI (YoY), IIP, GDP growth | MOSPI releases | monthly / quarterly |
| USDINR, Brent, India VIX, Nifty/500 level | yfinance (client already in-tree) | daily |

Contract: `source`, `indicator` (controlled vocab), `period` (the date the
reading *refers to*), `value`, `unit`, `released_at` (**when it was published —
the point-in-time anchor**), `fetched_at`, `content_hash`. The
`period`/`released_at` split matters: April CPI is released mid-May, so an as_of
of May 1 must **not** see it. Filter on `released_at <= as_of`, never `period`.

Both ingesters are pure plumbing and carry no permission gate.

---

## 2. Storage schema (deterministic)

Two ingestion tables above, plus the synthesis sink:

```
context_digests                       -- append-only, immutable (like snapshots)
  id                pk
  scope             'market' | 'stock'
  stock_id          fk stocks (NULL for market scope)
  as_of             timestamptz        -- the information cutoff this digest froze
  digest            text               -- the synthesized narrative (LLM output)
  highlights        jsonb              -- [{claim, source_item_id}] for provenance
  source_item_ids   jsonb              -- exact news_items behind this digest
  macro_ids         jsonb              -- exact macro_indicators behind it
  model             varchar(64)        -- what synthesized it
  prompt_version    varchar(32)
  context_hash      varchar(64)        -- hash(scope, stock_id, as_of, source ids)
  created_at        timestamptz
  unique (scope, stock_id, as_of)
```

`context_hash` is the join key into `analyses` (see §4). A digest is **frozen**:
once written for an `as_of` it is never rewritten (a corrected upstream item
produces a *new* digest at a later `as_of`, never a mutation of the old one).
This is what lets a historical re-run reproduce exactly the context a verdict saw.

`analyses` gains one nullable column — `context_hash varchar(64)` (+ optional FK
`context_id`). Nullable so the entire existing corpus is unaffected and
context-free analyses stay valid; the natural key is unchanged. Staleness
becomes: a verdict is stale iff its `data_hash` **or** its `context_hash` no
longer matches the latest. (Alternative discussed in §4.)

---

## 3. LLM synthesis layer — **PERMISSION-GATED**

> This layer does not run without explicit owner approval. The estimates below
> exist so that approval is an informed decision about quota, not a surprise.

The synthesizer turns raw `news_items` + `macro_indicators` (filtered to
`published_at`/`released_at <= as_of`) into a short, attributed digest. It is the
only LLM component; ingestion and storage never call a model. It must emit
`highlights` linking each claim to a `source_item_id` so the digest is auditable
and hallucinated context is detectable.

### Option A — market-level daily brief (recommended first)

One digest per day, `scope='market'`, `stock_id=NULL`: rate environment, index
tape, top sector moves, macro releases of the day.

- **Volume:** 1 call/day ≈ 30/month.
- **Tokens:** ~10–20k input (a day of macro + headline items), ~1k output.
- **Blast radius:** every stock's prompt references the *same* daily brief, so
  provenance is trivial and the quota cost is negligible.
- **Value:** gives all personas the macro backdrop the review flagged as missing,
  at almost no cost. Start here.

### Option B — per-stock news digest (gate hard, roll out in slices)

One digest per stock per cadence, `scope='stock'`: that name's announcements,
results, rating actions since the last digest.

- **Volume (daily):** ~600 scored stocks → **600 calls/day**, on top of the
  existing 10-persona analysis load. Heavy on Claude Max quota.
- **Volume (weekly):** ~600/7 ≈ **85/day**. Materially cheaper; most SME news is
  not daily anyway.
- **Volume (event-driven):** only synthesize a stock when a *new* `news_item`
  arrives for it — likely **< 100/day** across the universe and the best
  cost/signal trade. Recommended shape if B is approved at all.
- **Tokens:** ~3–8k input, ~500 output per call.

Recommendation: ship **A** behind approval; treat **B** as a separate approval,
event-driven, rolled out to a small universe slice first with a hard daily cap
enforced by the scheduler (reuse the Task-13 back-off accounting).

---

## 4. Entering the persona prompt without breaking point-in-time discipline

This is the load-bearing part. Four rules:

1. **Filter to the cutoff.** A digest for `as_of = T` includes only items with
   `published_at <= T` and indicators with `released_at <= T`. For live analysis
   `T = now`; for a historical re-run `T` is the analysis's information date. No
   item published after `T` can ever enter — this is enforced in the query, not
   trusted to the model.

2. **Freeze and hash.** The digest is written once per `(scope, stock_id, as_of)`
   and never mutated. Its `context_hash` covers the exact source-item id set, so
   two runs at the same `as_of` produce the same context or the hash reveals they
   didn't.

3. **Record what was seen.** The persona `Analysis` stores the `context_hash` it
   consumed, next to its `data_hash`. The corpus therefore always answers "what
   context did this verdict have?" — the same guarantee snapshots give for
   fundamentals. A verdict with `context_hash IS NULL` is, unambiguously, one
   formed without context (the whole current corpus).

4. **Present it as attributed context, not fact.** The digest enters the prompt
   as a delimited, dated, sourced block:

   ```
   ── MARKET CONTEXT (as of 2026-07-21, synthesized from 7 sources) ──
   Repo rate 6.00% (RBI, 2026-06-06). Nifty 500 +2.1% MoM. Chemicals sector
   rallied on ... [each claim carries a source id]
   ── STOCK NEWS (CUPID, as of 2026-07-21) ──
   Q4 results 2026-05-14: revenue +18% ... [source id]
   ```

   It never masquerades as a measured fundamental, and the persona is told it is
   synthesized context with a cutoff — consistent with finding **T** in the DQ
   roadmap (imputed inputs must be labelled, not laundered).

### Staleness policy (a real decision for the owner)

- **Strict:** context change re-triggers analysis (verdict stale iff `data_hash`
  *or* `context_hash` moved). Most correct; most expensive (news moves daily).
- **Advisory (recommended default):** context is recorded on the verdict but
  only `data_hash` drives re-analysis; a context refresh alone does not. Cheaper,
  and matches how a human analyst treats background reading. The `context_hash`
  is still stored, so a later strict backtest can measure whether context
  *would* have changed the call.

Either way the invariant holds: **every verdict records the exact context it
saw, and no verdict can see the future.**

---

## Build order

1. §1 + §2 ingestion and storage — deterministic, no permission needed. Ship the
   `news_items` / `macro_indicators` / `context_digests` tables and the
   ingesters; wire them into the scheduler with their own SLOs (DQ roadmap N4).
2. Add the nullable `context_hash` to `analyses`. No backfill; existing corpus
   stays context-free.
3. **Stop.** Get owner approval for §3 Option A. Run the market brief; verify the
   attribution block and `context_hash` recording end-to-end on a handful of
   stocks before any wider run.
4. Separate approval for Option B, event-driven, capped, small slice first.
