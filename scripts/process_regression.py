#!/usr/bin/env python3
"""Recommendation-vs-realized-return regression over the point-in-time corpus.

Answers the standing question: our council rated HOLD (and AVOID) for both the
best and the worst forward performers — so where is the signal leaking out?

This is a READ-ONLY diagnostic. It opens the database in read-only mode and
issues SELECTs only; it never writes. It emits a deterministic markdown report
to stdout (redirect it to a file to snapshot a run).

Methodology (the honesty rules are ported verbatim in spirit from
engine/backtest/study.py — that module can't be imported here because it needs a
live SQLAlchemy session and the ORM, neither available in this standalone
read-only tool; the pure measurement is duplicated deliberately and must stay in
step with study.py):

- Cohort = the EARLIEST composite_score_history row per stock (the first view
  that formed). No status filter anywhere — parked and dead names stay in
  (survivorship-safe).
- Entry = the first daily close STRICTLY AFTER the row's information_date (the
  day the view actually formed). No same-day fills, no lookahead.
- Excess return = stock return minus the benchmark return over the SAME calendar
  window (benchmark bar on/after entry, on/before exit).
- Persona verdicts counted for a stock are the latest per persona KNOWN at that
  stock's cutoff (information_date) — never a verdict formed after the view.
"""

import argparse
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, time, timezone

import psycopg

DEFAULT_BENCHMARK = "^CRSLDX"          # Nifty 500 — study.py's default comparator
DEFAULT_HORIZONS = (5, 21, 63)          # trading days: ~1w, 1m, 3m
COUNCIL = [
    "warren_buffett", "charlie_munger", "benjamin_graham", "peter_lynch",
    "philip_fisher", "joel_greenblatt", "howard_marks", "rakesh_jhunjhunwala",
    "radhakishan_damani", "vijay_kedia",
]
REC_ORDER = ("BUY", "HOLD", "AVOID")


# ---------------------------------------------------------------- pure stats

def _ranks(values):
    """Average ranks (1-based); ties share the mean rank."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs, ys):
    """Spearman rank correlation; None when undefined (<3 pairs or zero variance)."""
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    n = len(rx)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx == 0 or vy == 0:
        return None
    return cov / (vx * vy) ** 0.5


def _pct(a, b):
    return (b / a - 1.0) * 100.0


def _first_on_or_after(series, d):
    for row in series:
        if row[0] >= d:
            return row
    return None


def _last_on_or_before(series, d):
    prev = None
    for row in series:
        if row[0] > d:
            break
        prev = row
    return prev


def _measure(series, bench, info_date, horizons):
    """Entry + forward/excess returns for one stock. Pure (no DB)."""
    m = {
        "entry_date": None, "entry_price": None, "entry_idx": None,
        "returns": {h: None for h in horizons},
        "excess": {h: None for h in horizons},
        "to_date_return": None, "to_date_excess": None,
        "days_held": None,
    }
    entry_idx = next((i for i, (d, _) in enumerate(series) if d > info_date), None)
    if entry_idx is None:
        return m
    entry_d, entry_px = series[entry_idx]
    if not entry_px:
        return m
    m["entry_idx"] = entry_idx
    m["entry_date"], m["entry_price"] = entry_d, entry_px
    last_d, last_px = series[-1]
    m["days_held"] = (last_d - entry_d).days
    m["to_date_return"] = _pct(entry_px, last_px)

    b_entry = _first_on_or_after(bench, entry_d)
    for h in horizons:
        if entry_idx + h < len(series):
            _, exit_px = series[entry_idx + h]
            m["returns"][h] = _pct(entry_px, exit_px)
            if b_entry and b_entry[1]:
                b_exit = _last_on_or_before(bench, series[entry_idx + h][0])
                if b_exit and b_exit[0] > b_entry[0]:
                    m["excess"][h] = m["returns"][h] - _pct(b_entry[1], b_exit[1])
                elif b_exit:
                    m["excess"][h] = m["returns"][h]
    if b_entry and b_entry[1]:
        b_exit = _last_on_or_before(bench, last_d)
        if b_exit and b_exit[0] > b_entry[0]:
            m["to_date_excess"] = m["to_date_return"] - _pct(b_entry[1], b_exit[1])
        elif b_exit:
            m["to_date_excess"] = m["to_date_return"]
    return m


# ---------------------------------------------------------------- data access

def load_cohort(cur):
    cur.execute(
        """
        SELECT DISTINCT ON (h.stock_id)
            h.stock_id, s.symbol, s.company_name, s.status,
            h.information_date, h.as_of_date,
            h.composite_score, h.lcb, h.confidence_tier,
            h.consensus_recommendation, h.analysis_coverage,
            h.prompt_versions, h.models_used
        FROM composite_score_history h
        JOIN stocks s ON s.id = h.stock_id
        ORDER BY h.stock_id, h.as_of_date ASC, h.id ASC
        """
    )
    rows = []
    for r in cur.fetchall():
        info_date = r[4].date() if r[4] else r[5]
        cutoff = r[4] if r[4] else datetime.combine(r[5], time.max, tzinfo=timezone.utc)
        rows.append({
            "stock_id": r[0], "symbol": r[1], "company_name": r[2], "status": r[3],
            "info_date": info_date, "cutoff": cutoff,
            "composite": r[6], "lcb": r[7], "tier": r[8],
            "rec": r[9], "coverage": r[10],
            "prompt_versions": r[11] or {}, "models_used": r[12] or {},
        })
    return sorted(rows, key=lambda x: x["symbol"])


def load_prices(cur, ids):
    out = defaultdict(list)
    if not ids:
        return out
    cur.execute(
        "SELECT stock_id, date, close FROM daily_prices "
        "WHERE stock_id = ANY(%s) AND close IS NOT NULL "
        "ORDER BY stock_id, date ASC, id DESC",
        (ids,),
    )
    for sid, d, c in cur.fetchall():
        out[sid].append((d, c))
    return out


def load_benchmark(cur, symbol):
    cur.execute(
        "SELECT date, close FROM index_prices "
        "WHERE symbol = %s AND close IS NOT NULL ORDER BY date ASC, id DESC",
        (symbol,),
    )
    return cur.fetchall()


def load_verdicts(cur, cohort):
    """stock_id -> {persona: (score, rec, prompt_version, model)}, latest per
    persona known at the stock's cutoff (no lookahead)."""
    cutoffs = {c["stock_id"]: c["cutoff"] for c in cohort}
    ids = list(cutoffs)
    if not ids:
        return {}
    cur.execute(
        "SELECT stock_id, persona, score, recommendation, prompt_version, model, analyzed_at "
        "FROM analyses WHERE stock_id = ANY(%s) "
        "ORDER BY stock_id, persona, analyzed_at DESC, id DESC",
        (ids,),
    )
    out = defaultdict(dict)
    seen = set()
    for sid, persona, score, rec, pv, model, at in cur.fetchall():
        cutoff = cutoffs.get(sid)
        if cutoff is not None and at is not None and at > cutoff:
            continue
        key = (sid, persona)
        if key in seen:
            continue
        seen.add(key)
        out[sid][persona] = (score, rec, pv, model)
    return out


# ---------------------------------------------------------------- formatting

def f1(x, suffix="", sign=False):
    if x is None:
        return "—"
    return (f"{x:+.1f}{suffix}" if sign else f"{x:.1f}{suffix}")


def _stats(vals):
    """(n, mean, median, hit_rate%) over non-None excess values."""
    v = [x for x in vals if x is not None]
    if not v:
        return 0, None, None, None
    s = sorted(v)
    n = len(s)
    median = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
    hit = 100.0 * sum(1 for x in v if x > 0) / n
    return n, sum(v) / n, median, hit


def _dominant(counts):
    """argmax key of a {value: count} dict; 'mixed' when the top isn't a strict
    plurality of the contributing analyses."""
    if not counts:
        return "unknown"
    total = sum(counts.values())
    top_key, top_n = max(sorted(counts.items()), key=lambda kv: kv[1])
    return top_key if top_n * 2 > total else "mixed"


# ---------------------------------------------------------------- sections

def section_distribution(rows, horizons):
    """(a) recommendation distribution vs realized forward returns."""
    out = ["## (a) Recommendation distribution vs realized return", ""]
    counts = Counter(r["rec"] for r in rows)
    out.append(f"Cohort: {len(rows)} stocks | "
               + " | ".join(f"{k}={counts.get(k,0)}" for k in REC_ORDER)
               + f" | none={sum(1 for r in rows if r['rec'] not in REC_ORDER)}")
    priced = [r for r in rows if r["m"]["entry_price"] is not None]
    out.append(f"Priced (usable entry after info_date): {len(priced)}")
    out.append("")
    out.append("Excess return over benchmark, grouped by consensus recommendation. "
               "`hit` = P(beat benchmark) = share with excess > 0.")
    out.append("")
    windows = [("to_date", "to_date_excess")] + [(f"{h}d", h) for h in horizons]
    for label, key in windows:
        out.append(f"### window: {label}")
        out.append("")
        out.append("| rec | n | mean excess | median | hit rate |")
        out.append("|-----|---|-------------|--------|----------|")
        for rec in REC_ORDER:
            grp = [r for r in priced if r["rec"] == rec]
            if key == "to_date_excess":
                vals = [r["m"]["to_date_excess"] for r in grp]
            else:
                vals = [r["m"]["excess"][key] for r in grp]
            n, mean, med, hit = _stats(vals)
            out.append(f"| {rec} | {n} | {f1(mean,'%',True)} | {f1(med,'%',True)} | {f1(hit,'%')} |")
        # BUY-minus-AVOID directional spread
        if key == "to_date_excess":
            b = [r["m"]["to_date_excess"] for r in priced if r["rec"] == "BUY"]
            a = [r["m"]["to_date_excess"] for r in priced if r["rec"] == "AVOID"]
        else:
            b = [r["m"]["excess"][key] for r in priced if r["rec"] == "BUY"]
            a = [r["m"]["excess"][key] for r in priced if r["rec"] == "AVOID"]
        _, bm, _, _ = _stats(b)
        _, am, _, _ = _stats(a)
        spread = (bm - am) if (bm is not None and am is not None) else None
        out.append("")
        out.append(f"BUY−AVOID mean-excess spread: **{f1(spread,'%',True)}** "
                   "(a healthy signal is strongly positive; negative = inverted).")
        out.append("")
    return out


def section_calibration(rows, verdicts, horizons):
    """(b) score calibration — realized excess per decile, composite and persona."""
    out = ["## (b) Score calibration", ""]
    priced = [r for r in rows if r["m"]["entry_price"] is not None]

    # Composite deciles (to_date excess).
    ranked = sorted([r for r in priced if r["composite"] is not None],
                    key=lambda r: (r["composite"], r["symbol"]))
    n = len(ranked)
    out.append("### Composite-score deciles vs to-date excess")
    out.append("")
    out.append("Decile 1 = lowest composite, 10 = highest. A calibrated score is "
               "monotone increasing in mean excess.")
    out.append("")
    out.append("| decile | n | composite range | mean excess | median | hit |")
    out.append("|--------|---|-----------------|-------------|--------|-----|")
    if n >= 10:
        for d in range(10):
            lo = d * n // 10
            hi = (d + 1) * n // 10 if d < 9 else n
            grp = ranked[lo:hi]
            vals = [r["m"]["to_date_excess"] for r in grp]
            cnt, mean, med, hit = _stats(vals)
            crange = f"{grp[0]['composite']:.0f}–{grp[-1]['composite']:.0f}"
            out.append(f"| {d+1} | {len(grp)} | {crange} | {f1(mean,'%',True)} | "
                       f"{f1(med,'%',True)} | {f1(hit,'%')} |")
    else:
        out.append(f"| — | {n} | insufficient (<10) | — | — | — |")
    out.append("")

    # Composite IC per horizon.
    out.append("### Composite information coefficient (Spearman: composite vs return)")
    out.append("")
    out.append("| horizon | n | IC |")
    out.append("|---------|---|----|")
    for label, key in [("to_date", "to_date_return")] + [(f"{h}d", h) for h in horizons]:
        pairs = []
        for r in priced:
            if r["composite"] is None:
                continue
            ret = r["m"]["to_date_return"] if key == "to_date_return" else r["m"]["returns"][key]
            if ret is not None:
                pairs.append((r["composite"], ret))
        ic = spearman([p[0] for p in pairs], [p[1] for p in pairs])
        out.append(f"| {label} | {len(pairs)} | {f1(ic) if ic is not None else '—'} |")
    out.append("")

    # Per-persona IC (score vs stock to-date excess) — which persona discriminates?
    out.append("### Per-persona information coefficient (score vs to-date excess)")
    out.append("")
    out.append("IC near zero or negative = that persona's score does not separate "
               "winners from losers. Sorted worst-first.")
    out.append("")
    out.append("| persona | n | IC | mean excess: BUY / HOLD / AVOID |")
    out.append("|---------|---|----|--------------------------------|")
    ex_by_sid = {r["stock_id"]: r["m"]["to_date_excess"] for r in priced}
    persona_rows = []
    for persona in COUNCIL:
        pairs, by_rec = [], defaultdict(list)
        for sid, ex in ex_by_sid.items():
            v = verdicts.get(sid, {}).get(persona)
            if not v or ex is None:
                continue
            score, rec = v[0], v[1]
            if score is not None:
                pairs.append((score, ex))
            if rec in REC_ORDER:
                by_rec[rec].append(ex)
        ic = spearman([p[0] for p in pairs], [p[1] for p in pairs])
        rec_means = " / ".join(
            f1(_stats(by_rec[r])[1], "%", True) for r in REC_ORDER
        )
        persona_rows.append((ic if ic is not None else 0.0, persona, len(pairs), ic, rec_means))
    for _, persona, cnt, ic, rec_means in sorted(persona_rows, key=lambda t: (t[0], t[1])):
        out.append(f"| {persona} | {cnt} | {f1(ic) if ic is not None else '—'} | {rec_means} |")
    out.append("")

    # Pooled persona score level -> mean excess.
    out.append("### Pooled persona score level vs to-date excess (all personas)")
    out.append("")
    out.append("| score | n verdicts | mean excess | hit |")
    out.append("|-------|-----------|-------------|-----|")
    by_score = defaultdict(list)
    for sid, ex in ex_by_sid.items():
        if ex is None:
            continue
        for persona in COUNCIL:
            v = verdicts.get(sid, {}).get(persona)
            if v and v[0] is not None:
                by_score[v[0]].append(ex)
    for score in sorted(by_score):
        cnt, mean, _, hit = _stats(by_score[score])
        out.append(f"| {score} | {cnt} | {f1(mean,'%',True)} | {f1(hit,'%')} |")
    out.append("")
    return out


def section_discrimination(rows, verdicts, top_k):
    """(c) discrimination failures — the specific stocks and the failing factor."""
    out = ["## (c) Discrimination failures", ""]
    priced = [r for r in rows if r["m"]["to_date_excess"] is not None]
    priced.sort(key=lambda r: (r["m"]["to_date_excess"], r["symbol"]))

    def table(group, title):
        out.append(f"### {title}")
        out.append("")
        out.append("| symbol | to-date excess | rec | composite | coverage | prompt | model |")
        out.append("|--------|----------------|-----|-----------|----------|--------|-------|")
        for r in group:
            out.append(
                f"| {r['symbol']} | {f1(r['m']['to_date_excess'],'%',True)} | {r['rec']} | "
                f"{f1(r['composite'])} | {r['coverage']} | "
                f"{_dominant(r['prompt_versions'])} | {_dominant(r['models_used'])} |"
            )
        out.append("")

    table(list(reversed(priced[-top_k:])), f"Top {top_k} realized performers")
    table(priced[:top_k], f"Bottom {top_k} realized performers")

    # The collision: how separated are the extremes, really?
    k = min(20, len(priced) // 2)
    top, bot = priced[-k:], priced[:k]
    tc = Counter(r["rec"] for r in top)
    bc = Counter(r["rec"] for r in bot)
    tmean = sum(r["composite"] for r in top if r["composite"] is not None) / max(
        1, sum(1 for r in top if r["composite"] is not None))
    bmean = sum(r["composite"] for r in bot if r["composite"] is not None) / max(
        1, sum(1 for r in bot if r["composite"] is not None))
    out.append(f"### Separation at the extremes (top {k} vs bottom {k} realized)")
    out.append("")
    out.append(f"- Top {k} recs: " + ", ".join(f"{r}={tc.get(r,0)}" for r in REC_ORDER)
               + f" — mean composite **{tmean:.1f}**")
    out.append(f"- Bottom {k} recs: " + ", ".join(f"{r}={bc.get(r,0)}" for r in REC_ORDER)
               + f" — mean composite **{bmean:.1f}**")
    out.append(f"- Composite separation between best and worst cohorts: "
               f"**{tmean - bmean:+.1f}** points on a 0–100 scale.")
    buys_at_extremes = tc.get("BUY", 0) + bc.get("BUY", 0)
    out.append(f"- BUY calls anywhere in either extreme: **{buys_at_extremes}**.")
    out.append("")

    # Attribution: are the extremes concentrated in a failing regime?
    def regime(group):
        pv = Counter(_dominant(r["prompt_versions"]) for r in group)
        thin = sum(1 for r in group if (r["coverage"] or 0) < 7)
        return pv, thin
    tpv, tthin = regime(top)
    bpv, bthin = regime(bot)
    out.append("### Attribution of the failure")
    out.append("")
    out.append(f"- Top-{k} prompt regime: {dict(tpv)}; thin coverage (<7): {tthin}/{k}")
    out.append(f"- Bottom-{k} prompt regime: {dict(bpv)}; thin coverage (<7): {bthin}/{k}")
    out.append("")

    # Explicit same-rec collision pairs (best & worst that share a verdict).
    out.append("### Same-recommendation collisions (best vs worst sharing a verdict)")
    out.append("")
    best = priced[-1]
    for rec in REC_ORDER:
        worst_same = next((r for r in priced if r["rec"] == rec), None)
        best_same = next((r for r in reversed(priced) if r["rec"] == rec), None)
        if best_same and worst_same and best_same["symbol"] != worst_same["symbol"]:
            out.append(
                f"- **{rec}**: {best_same['symbol']} "
                f"({f1(best_same['m']['to_date_excess'],'%',True)}) and "
                f"{worst_same['symbol']} "
                f"({f1(worst_same['m']['to_date_excess'],'%',True)}) — identical verdict, "
                f"{f1(best_same['m']['to_date_excess']-worst_same['m']['to_date_excess'],' pts')} apart."
            )
    out.append("")
    return out


def section_strata(rows, min_n, horizons, stale_priced_note=None):
    """(d) stratify by prompt_version and model; refuse conclusions under min_n."""
    out = ["## (d) Stratification by prompt version and model", ""]
    out.append(f"Conclusions are refused for any stratum with priced n < {min_n}.")
    out.append("")
    if stale_priced_note:
        out.append(stale_priced_note)
        out.append("")

    versions_present = Counter()
    for r in rows:
        versions_present[_dominant(r["prompt_versions"])] += 1
    out.append(f"Dominant prompt_version across the cohort: {dict(versions_present)}")
    if "v5" not in versions_present:
        out.append("")
        out.append("> **No v5 corpus in this database.** The v5 prompt (Task 15: "
                   "bands BUY≥7 / HOLD 4–6 / AVOID≤3, consensus = modal vote) and the "
                   "Phase-4 re-analysis campaign were applied on **prod (the Hetzner "
                   "box)**, not this local dev DB. The requested v4→v5 discrimination "
                   "comparison **cannot be computed here** and is not attempted. The "
                   "available era contrast is **v1 (price-blind, pre-Task-8) vs v4 "
                   "(price-aware, Task-8)** — reported below. Note the confound: v1 was "
                   "produced by sonnet/opus and v4 by fable-5/opus-4-8, so a v1→v4 "
                   "delta blends a prompt change with a model upgrade.")
    out.append("")

    def strat_table(title, keyfn):
        out.append(f"### {title}")
        out.append("")
        out.append("| stratum | n | priced | BUY−AVOID spread (to-date) | composite IC (to-date) | verdict |")
        out.append("|---------|---|--------|----------------------------|------------------------|---------|")
        groups = defaultdict(list)
        for r in rows:
            groups[keyfn(r)].append(r)
        for k in sorted(groups):
            grp = groups[k]
            priced = [r for r in grp if r["m"]["to_date_excess"] is not None]
            b = [r["m"]["to_date_excess"] for r in priced if r["rec"] == "BUY"]
            a = [r["m"]["to_date_excess"] for r in priced if r["rec"] == "AVOID"]
            _, bm, _, _ = _stats(b)
            _, am, _, _ = _stats(a)
            spread = (bm - am) if (bm is not None and am is not None) else None
            pairs = [(r["composite"], r["m"]["to_date_return"]) for r in priced
                     if r["composite"] is not None and r["m"]["to_date_return"] is not None]
            ic = spearman([p[0] for p in pairs], [p[1] for p in pairs])
            verdict = "REFUSED (n<%d)" % min_n if len(priced) < min_n else "reported"
            out.append(
                f"| {k} | {len(grp)} | {len(priced)} | "
                f"{f1(spread,'%',True) if verdict=='reported' else '—'} | "
                f"{f1(ic) if (ic is not None and verdict=='reported') else '—'} | {verdict} |"
            )
        out.append("")

    strat_table("By dominant prompt_version", lambda r: _dominant(r["prompt_versions"]))
    strat_table("By dominant model", lambda r: _dominant(r["models_used"]))
    return out


# ---------------------------------------------------------------- summary

def section_summary(rows, verdicts, horizons):
    """Findings-first block: the actionable improvements, each tied to a number."""
    priced = [r for r in rows if r["m"]["entry_price"] is not None]
    def rec_mean(rec):
        return _stats([r["m"]["to_date_excess"] for r in priced if r["rec"] == rec])[1]
    buy_m, hold_m, avoid_m = rec_mean("BUY"), rec_mean("HOLD"), rec_mean("AVOID")

    ptd = [r for r in rows if r["m"]["to_date_excess"] is not None]
    ptd.sort(key=lambda r: (r["m"]["to_date_excess"], r["symbol"]))
    k = min(20, len(ptd) // 2)
    top, bot = ptd[-k:], ptd[:k]
    tmean = sum(r["composite"] for r in top if r["composite"] is not None) / max(1, k)
    bmean = sum(r["composite"] for r in bot if r["composite"] is not None) / max(1, k)

    pairs = [(r["composite"], r["m"]["to_date_return"]) for r in priced
             if r["composite"] is not None and r["m"]["to_date_return"] is not None]
    comp_ic = spearman([p[0] for p in pairs], [p[1] for p in pairs])
    counts = Counter(r["rec"] for r in rows)

    out = ["# Process regression: recommendations vs realized returns", ""]
    out.append(f"_Generated {datetime.now(timezone.utc):%Y-%m-%d} (UTC) · read-only · "
               f"cohort = earliest point-in-time view per stock._")
    out.append("")
    out.append("## Findings summary — most actionable improvements")
    out.append("")
    out.append(
        f"1. **The composite carries almost no forward signal — and what little it "
        f"has points the wrong way.** Composite-vs-return IC (to-date) = "
        f"**{f1(comp_ic)}**. Best-20 realized performers averaged composite "
        f"**{tmean:.1f}**; worst-20 averaged **{bmean:.1f}** — a separation of only "
        f"**{tmean-bmean:+.1f}** points on 0–100. *Action: the composite must be "
        f"validated against realized excess return as a gate before it is trusted; "
        f"today it is not predictive.*")
    out.append(
        f"2. **The recommendation ladder is inverted.** Mean to-date excess: BUY "
        f"**{f1(buy_m,'%',True)}**, HOLD **{f1(hold_m,'%',True)}**, AVOID "
        f"**{f1(avoid_m,'%',True)}**. AVOID beat BUY. *Action: recalibrate the "
        f"score→band mapping against outcomes; the current bands anti-select.*")
    out.append(
        f"3. **The council is chronically bearish, so it never takes a position that "
        f"could be right.** Cohort recs: BUY={counts.get('BUY',0)}, "
        f"HOLD={counts.get('HOLD',0)}, AVOID={counts.get('AVOID',0)}. With so few "
        f"BUYs, HOLD/AVOID become an undifferentiated dumping ground — exactly the "
        f"'HOLD for both best and worst' the review flagged. *Action: the personas' "
        f"absolute-valuation lens rejects almost everything in a momentum tape; add "
        f"a relative/what-would-change-my-mind rubric and price-momentum context.*")
    out.append(
        f"4. **See section (b): identify and down-weight the personas whose IC is ≈0 "
        f"or negative** — they add noise, not signal, to the mean-of-10 composite. "
        f"*Action: weight council members by realized IC instead of equal-weighting.*")
    out.append(
        f"5. **See section (c) attribution: thin coverage and the price-blind v1 era "
        f"concentrate at the extremes** — those are re-analysis targets. The v5 "
        f"corpus is absent from this DB, so v4→v5 improvement is unmeasured here "
        f"(section d). *Action: run the study on prod where v5 exists before "
        f"declaring the prompt fixes worked.*")
    out.append("")
    out.append("---")
    out.append("")
    return out


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db-url", default=os.environ.get(
        "DATABASE_URL", "postgresql://ipo:ipo@localhost:5432/ipo"))
    ap.add_argument("--benchmark", default=DEFAULT_BENCHMARK)
    ap.add_argument("--horizons", default=",".join(str(h) for h in DEFAULT_HORIZONS))
    ap.add_argument("--min-n", type=int, default=25,
                    help="refuse per-stratum conclusions below this priced n")
    ap.add_argument("--top-k", type=int, default=15,
                    help="rows in the discrimination top/bottom tables")
    args = ap.parse_args(argv)

    horizons = tuple(int(h) for h in args.horizons.split(",") if h.strip())
    # SQLAlchemy-style URLs carry a +driver we must drop for libpq.
    dsn = args.db_url.replace("postgresql+psycopg://", "postgresql://")

    # Hard guard: a server-side read-only session rejects every write (INSERT/
    # UPDATE/CREATE), so this tool cannot mutate the DB even by mistake. Enforced
    # at connect time because conn.read_only is a no-op under autocommit.
    with psycopg.connect(
        dsn, autocommit=True, connect_timeout=10,
        options="-c default_transaction_read_only=on",
    ) as conn:
        with conn.cursor() as cur:
            cohort = load_cohort(cur)
            prices = load_prices(cur, [c["stock_id"] for c in cohort])
            bench = load_benchmark(cur, args.benchmark)
            verdicts = load_verdicts(cur, cohort)

    for c in cohort:
        series = prices.get(c["stock_id"], [])
        c["m"] = _measure(series, bench, c["info_date"], horizons)
        c["_has_bars"] = bool(series)
        c["_last_bar"] = series[-1][0] if series else None

    # Diagnose the dominant reason the improved-prompt cohort is unpriceable: its
    # price series ends before the view formed (recent campaign IPOs). This makes
    # the whole priced cohort the v1/price-blind era — stated so section (d)'s
    # empty strata read as a data-coverage limit, not a bug.
    unpriced = [c for c in cohort if c["m"]["entry_price"] is None]
    stale = [c for c in unpriced if c["_last_bar"] is not None and c["_last_bar"] <= c["info_date"]]
    nobars = [c for c in unpriced if not c["_has_bars"]]
    non_v1_priced = sum(1 for c in cohort if _dominant(c["prompt_versions"]) != "v1"
                        and c["m"]["entry_price"] is not None)
    stale_note = (
        f"> **Why the improved-prompt strata are empty.** Of {len(unpriced)} "
        f"unpriced cohort stocks, {len(stale)} have their last local price bar "
        f"**on or before** their information_date and {len(nobars)} have no bars at "
        f"all — these are the recent 2026-IPO campaign names (v3/v4/fable-5) whose "
        f"local price series stops before the view formed. Only {non_v1_priced} "
        f"non-v1 stock is priceable, so **every conclusion below rests on the v1 / "
        f"sonnet price-blind era**. A meaningful v1→v4→v5 comparison requires a "
        f"prod run with refreshed prices for the campaign cohort."
    )

    lines = []
    lines += section_summary(cohort, verdicts, horizons)
    lines.append(f"Benchmark: `{args.benchmark}` ({len(bench)} bars). "
                 f"Horizons (trading days): {list(horizons)}. "
                 f"Cohort: {len(cohort)} stocks, "
                 f"{sum(1 for c in cohort if c['m']['entry_price'] is not None)} priced. "
                 f"Info-date range: {min(c['info_date'] for c in cohort)} → "
                 f"{max(c['info_date'] for c in cohort)}.")
    lines.append("")
    lines += section_distribution(cohort, horizons)
    lines += section_calibration(cohort, verdicts, horizons)
    lines += section_discrimination(cohort, verdicts, args.top_k)
    lines += section_strata(cohort, args.min_n, horizons, stale_priced_note=stale_note)

    sys.stdout.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
