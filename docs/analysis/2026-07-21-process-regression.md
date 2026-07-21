# Process regression: recommendations vs realized returns

_Generated 2026-07-21 (UTC) · read-only · cohort = earliest point-in-time view per stock._

## Findings summary — most actionable improvements

1. **The composite carries almost no forward signal — and what little it has points the wrong way.** Composite-vs-return IC (to-date) = **-0.1**. Best-20 realized performers averaged composite **32.9**; worst-20 averaged **31.1** — a separation of only **+1.9** points on 0–100. *Action: the composite must be validated against realized excess return as a gate before it is trusted; today it is not predictive.*
2. **The recommendation ladder is inverted.** Mean to-date excess: BUY **+4.7%**, HOLD **+5.4%**, AVOID **+8.0%**. AVOID beat BUY. *Action: recalibrate the score→band mapping against outcomes; the current bands anti-select.*
3. **The council is chronically bearish, so it never takes a position that could be right.** Cohort recs: BUY=14, HOLD=141, AVOID=417. With so few BUYs, HOLD/AVOID become an undifferentiated dumping ground — exactly the 'HOLD for both best and worst' the review flagged. *Action: the personas' absolute-valuation lens rejects almost everything in a momentum tape; add a relative/what-would-change-my-mind rubric and price-momentum context.*
4. **See section (b): identify and down-weight the personas whose IC is ≈0 or negative** — they add noise, not signal, to the mean-of-10 composite. *Action: weight council members by realized IC instead of equal-weighting.*
5. **See section (c) attribution: thin coverage and the price-blind v1 era concentrate at the extremes** — those are re-analysis targets. The v5 corpus is absent from this DB, so v4→v5 improvement is unmeasured here (section d). *Action: run the study on prod where v5 exists before declaring the prompt fixes worked.*

---

Benchmark: `^CRSLDX` (5117 bars). Horizons (trading days): [5, 21, 63]. Cohort: 572 stocks, 356 priced. Info-date range: 2026-04-18 → 2026-07-06.

## (a) Recommendation distribution vs realized return

Cohort: 572 stocks | BUY=14 | HOLD=141 | AVOID=417 | none=0
Priced (usable entry after info_date): 356

Excess return over benchmark, grouped by consensus recommendation. `hit` = P(beat benchmark) = share with excess > 0.

### window: to_date

| rec | n | mean excess | median | hit rate |
|-----|---|-------------|--------|----------|
| BUY | 13 | +4.7% | -0.9% | 46.2% |
| HOLD | 104 | +5.4% | +0.6% | 51.0% |
| AVOID | 239 | +8.0% | +4.3% | 59.4% |

BUY−AVOID mean-excess spread: **-3.3%** (a healthy signal is strongly positive; negative = inverted).

### window: 5d

| rec | n | mean excess | median | hit rate |
|-----|---|-------------|--------|----------|
| BUY | 13 | +1.8% | +0.4% | 61.5% |
| HOLD | 102 | +1.6% | +1.6% | 62.7% |
| AVOID | 239 | +2.7% | +1.6% | 68.2% |

BUY−AVOID mean-excess spread: **-0.9%** (a healthy signal is strongly positive; negative = inverted).

### window: 21d

| rec | n | mean excess | median | hit rate |
|-----|---|-------------|--------|----------|
| BUY | 13 | +1.7% | +2.7% | 53.8% |
| HOLD | 102 | +1.0% | -0.3% | 49.0% |
| AVOID | 239 | +3.2% | +1.7% | 56.9% |

BUY−AVOID mean-excess spread: **-1.5%** (a healthy signal is strongly positive; negative = inverted).

### window: 63d

| rec | n | mean excess | median | hit rate |
|-----|---|-------------|--------|----------|
| BUY | 0 | — | — | — |
| HOLD | 0 | — | — | — |
| AVOID | 0 | — | — | — |

BUY−AVOID mean-excess spread: **—** (a healthy signal is strongly positive; negative = inverted).

## (b) Score calibration

### Composite-score deciles vs to-date excess

Decile 1 = lowest composite, 10 = highest. A calibrated score is monotone increasing in mean excess.

| decile | n | composite range | mean excess | median | hit |
|--------|---|-----------------|-------------|--------|-----|
| 1 | 35 | 10–20 | +3.9% | -0.4% | 48.6% |
| 2 | 36 | 20–24 | +13.9% | +8.7% | 72.2% |
| 3 | 35 | 25–28 | +11.8% | +12.2% | 68.6% |
| 4 | 36 | 28–30 | +7.6% | +4.2% | 55.6% |
| 5 | 36 | 30–33 | +10.9% | +5.8% | 63.9% |
| 6 | 35 | 33–37 | +3.8% | +0.4% | 51.4% |
| 7 | 36 | 37–40 | +2.2% | +1.4% | 52.8% |
| 8 | 35 | 40–43 | +4.2% | +1.8% | 54.3% |
| 9 | 36 | 43–49 | +8.7% | -0.1% | 50.0% |
| 10 | 36 | 49–70 | +3.8% | -0.3% | 47.2% |

### Composite information coefficient (Spearman: composite vs return)

| horizon | n | IC |
|---------|---|----|
| to_date | 356 | -0.1 |
| 5d | 354 | -0.1 |
| 21d | 354 | -0.1 |
| 63d | 0 | — |

### Per-persona information coefficient (score vs to-date excess)

IC near zero or negative = that persona's score does not separate winners from losers. Sorted worst-first.

| persona | n | IC | mean excess: BUY / HOLD / AVOID |
|---------|---|----|--------------------------------|
| howard_marks | 235 | -0.2 | -8.3% / +4.4% / +11.0% |
| benjamin_graham | 277 | -0.2 | -2.6% / -1.2% / +8.4% |
| joel_greenblatt | 269 | -0.2 | -2.7% / +3.7% / +9.2% |
| warren_buffett | 356 | -0.1 | -5.3% / +3.8% / +8.8% |
| radhakishan_damani | 288 | -0.1 | — / +4.1% / +9.1% |
| charlie_munger | 255 | -0.1 | +22.3% / +4.1% / +10.1% |
| rakesh_jhunjhunwala | 264 | -0.0 | +1.5% / +7.2% / +8.7% |
| peter_lynch | 254 | 0.0 | +19.1% / +5.6% / +8.7% |
| vijay_kedia | 275 | 0.1 | — / +11.5% / +7.2% |
| philip_fisher | 278 | 0.1 | +5.3% / +9.4% / +6.4% |

### Pooled persona score level vs to-date excess (all personas)

| score | n verdicts | mean excess | hit |
|-------|-----------|-------------|-----|
| 1 | 119 | +5.1% | 48.7% |
| 2 | 659 | +10.4% | 63.0% |
| 3 | 843 | +8.0% | 59.4% |
| 4 | 572 | +5.4% | 52.8% |
| 5 | 351 | +6.4% | 55.8% |
| 6 | 143 | +6.8% | 49.7% |
| 7 | 59 | +8.8% | 52.5% |
| 8 | 5 | +1.9% | 40.0% |

## (c) Discrimination failures

### Top 15 realized performers

| symbol | to-date excess | rec | composite | coverage | prompt | model |
|--------|----------------|-----|-----------|----------|--------|-------|
| CEMPRO | +126.0% | HOLD | 47.0 | 10 | v1 | sonnet |
| HFCL | +117.3% | AVOID | 22.5 | 4 | v1 | sonnet |
| BALAMINES | +89.9% | AVOID | 37.0 | 10 | v1 | sonnet |
| BBOX | +87.1% | AVOID | 29.0 | 10 | v1 | sonnet |
| CUPID | +86.7% | AVOID | 16.0 | 10 | v1 | sonnet |
| AEGISLOG | +77.1% | HOLD | 47.0 | 10 | v1 | sonnet |
| ASTRAMICRO | +71.7% | AVOID | 32.0 | 10 | v1 | sonnet |
| GRWRHITECH | +70.9% | HOLD | 55.0 | 10 | v1 | sonnet |
| AVALON | +64.2% | AVOID | 23.0 | 10 | v1 | sonnet |
| PARAS | +63.7% | AVOID | 28.9 | 9 | v1 | sonnet |
| RATEGAIN | +63.3% | HOLD | 40.0 | 2 | v1 | sonnet |
| ICIL | +57.1% | AVOID | 27.0 | 10 | v1 | sonnet |
| CPPLUS | +56.0% | AVOID | 27.0 | 10 | v1 | sonnet |
| MSTCLTD | +55.0% | AVOID | 28.0 | 10 | v1 | sonnet |
| APOLLO | +53.7% | AVOID | 24.0 | 10 | v1 | sonnet |

### Bottom 15 realized performers

| symbol | to-date excess | rec | composite | coverage | prompt | model |
|--------|----------------|-----|-----------|----------|--------|-------|
| AVANTIFEED | -38.3% | HOLD | 40.0 | 10 | v1 | sonnet |
| PFOCUS | -29.9% | AVOID | 17.8 | 9 | v1 | sonnet |
| BSOFT | -27.9% | AVOID | 38.0 | 10 | v1 | sonnet |
| HEG | -23.3% | AVOID | 20.0 | 5 | v1 | sonnet |
| KAYNES | -23.0% | HOLD | 40.0 | 1 | v1 | sonnet |
| PINELABS | -22.4% | AVOID | 13.3 | 3 | v1 | sonnet |
| MRPL | -21.8% | AVOID | 20.0 | 1 | v1 | sonnet |
| JYOTHYLAB | -21.4% | HOLD | 43.0 | 10 | v1 | sonnet |
| GALLANTT | -20.4% | AVOID | 28.9 | 9 | v1 | sonnet |
| BAJAJELEC | -19.1% | AVOID | 21.0 | 10 | v1 | sonnet |
| LUMAXTECH | -17.0% | AVOID | 37.0 | 10 | v1 | sonnet |
| EIDPARRY | -17.0% | HOLD | 41.0 | 10 | v1 | sonnet |
| PNGJL | -16.9% | AVOID | 38.6 | 7 | v1 | sonnet |
| GRAPHITE | -16.9% | AVOID | 25.0 | 6 | v1 | sonnet |
| BAYERCROP | -16.7% | AVOID | 39.0 | 10 | v1 | sonnet |

### Separation at the extremes (top 20 vs bottom 20 realized)

- Top 20 recs: BUY=0, HOLD=6, AVOID=14 — mean composite **32.9**
- Bottom 20 recs: BUY=0, HOLD=4, AVOID=16 — mean composite **31.1**
- Composite separation between best and worst cohorts: **+1.9** points on a 0–100 scale.
- BUY calls anywhere in either extreme: **0**.

### Attribution of the failure

- Top-20 prompt regime: {'v1': 20}; thin coverage (<7): 3/20
- Bottom-20 prompt regime: {'v1': 20}; thin coverage (<7): 5/20

### Same-recommendation collisions (best vs worst sharing a verdict)

- **BUY**: IKS (+28.2%) and INDIAMART (-13.4%) — identical verdict, 41.6 pts apart.
- **HOLD**: CEMPRO (+126.0%) and AVANTIFEED (-38.3%) — identical verdict, 164.3 pts apart.
- **AVOID**: HFCL (+117.3%) and PFOCUS (-29.9%) — identical verdict, 147.1 pts apart.

## (d) Stratification by prompt version and model

Conclusions are refused for any stratum with priced n < 25.

> **Why the improved-prompt strata are empty.** Of 216 unpriced cohort stocks, 35 have their last local price bar **on or before** their information_date and 181 have no bars at all — these are the recent 2026-IPO campaign names (v3/v4/fable-5) whose local price series stops before the view formed. Only 0 non-v1 stock is priceable, so **every conclusion below rests on the v1 / sonnet price-blind era**. A meaningful v1→v4→v5 comparison requires a prod run with refreshed prices for the campaign cohort.

Dominant prompt_version across the cohort: {'v1': 453, 'v3': 10, 'v4': 109}

> **No v5 corpus in this database.** The v5 prompt (Task 15: bands BUY≥7 / HOLD 4–6 / AVOID≤3, consensus = modal vote) and the Phase-4 re-analysis campaign were applied on **prod (the Hetzner box)**, not this local dev DB. The requested v4→v5 discrimination comparison **cannot be computed here** and is not attempted. The available era contrast is **v1 (price-blind, pre-Task-8) vs v4 (price-aware, Task-8)** — reported below. Note the confound: v1 was produced by sonnet/opus and v4 by fable-5/opus-4-8, so a v1→v4 delta blends a prompt change with a model upgrade.

### By dominant prompt_version

| stratum | n | priced | BUY−AVOID spread (to-date) | composite IC (to-date) | verdict |
|---------|---|--------|----------------------------|------------------------|---------|
| v1 | 453 | 356 | -3.3% | -0.1 | reported |
| v3 | 10 | 0 | — | — | REFUSED (n<25) |
| v4 | 109 | 0 | — | — | REFUSED (n<25) |

### By dominant model

| stratum | n | priced | BUY−AVOID spread (to-date) | composite IC (to-date) | verdict |
|---------|---|--------|----------------------------|------------------------|---------|
| claude-fable-5 | 96 | 0 | — | — | REFUSED (n<25) |
| claude-opus-4-8 | 23 | 0 | — | — | REFUSED (n<25) |
| opus | 86 | 7 | — | — | REFUSED (n<25) |
| sonnet | 367 | 349 | -3.3% | -0.1 | reported |

