"use client";

import Link from "next/link";
import { useCallback, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { api } from "@/lib/api";
import { useAsync, useDebounced, useLocalStorage } from "@/lib/hooks";
import type {
  BacktestBucket,
  BacktestStock,
  BacktestStudy,
  CI,
  CohortBlock,
  PersonaStudy,
  Verdict,
  VintageStudy,
} from "@/lib/types";
import { composite, compositeColor, pct, shortDate, DASH } from "@/lib/format";
import { PERSONA_ORDER, PERSONA_SLUGS } from "@/lib/personas";
import StatCard from "@/components/StatCard";
import DataTable, { Column, SortState } from "@/components/DataTable";
import RecChip from "@/components/RecChip";
import TierChip from "@/components/TierChip";
import PersonaWeighting from "@/components/PersonaWeighting";
import EquityCurve from "@/components/EquityCurve";
import VintageSignals from "@/components/VintageSignals";
import PersonaRanking, { Metric } from "@/components/PersonaRanking";
import { Panel, PanelHeader } from "@/components/Panel";
import { ErrorState, Skeleton, TableSkeleton } from "@/components/States";

const TIER_ORDER = ["high", "moderate", "mixed", "provisional"];
const QUINTILE_ORDER = ["5", "4", "3", "2", "1"];

// Plain-language names for the trading-day horizons (spelled out so nobody has
// to know that 21 bars ≈ a month).
const HORIZON_LABEL: Record<number, string> = {
  5: "1 week",
  21: "1 month",
  63: "3 months",
  126: "6 months",
};
function horizonLabel(h: number): string {
  return HORIZON_LABEL[h] ?? `${h} days`;
}
function horizonTitle(h: number): string {
  return `${h} trading days${HORIZON_LABEL[h] ? ` ≈ ${HORIZON_LABEL[h]}` : ""}`;
}

function signColor(v: number | null | undefined): string {
  if (v == null) return "var(--color-muted)";
  return v >= 0 ? "var(--color-sage)" : "var(--color-terracotta)";
}

const VERDICT_LABEL: Record<Verdict, string> = {
  positive: "positive",
  negative: "negative",
  "indistinguishable from zero": "≈ zero",
};

function verdictColor(v: Verdict): string {
  if (v === "positive") return "var(--color-sage)";
  if (v === "negative") return "var(--color-terracotta)";
  return "var(--color-muted)";
}

function isNoise(ci: CI | null | undefined): boolean {
  return ci != null && ci.verdict === "indistinguishable from zero";
}

// Full interval + verdict for a tooltip, e.g. "95% CI −1.2 to +4.8 · ≈ zero".
function ciRange(ci: CI | null | undefined): string {
  if (!ci) return "too few names to bootstrap a CI";
  const f = (x: number) => `${x > 0 ? "+" : ""}${x.toFixed(1)}`;
  return `95% CI ${f(ci.ci_low)} to ${f(ci.ci_high)} · ${ci.verdict}`;
}

export default function BacktestPage() {
  const studyFetcher = useCallback((s: AbortSignal) => api.backtest(s), []);
  const { data, loading, error, refetch } = useAsync<BacktestStudy>(
    studyFetcher,
    [],
  );

  // Council subset for the equity curve + per-persona comparison. Persisted so
  // a chosen lens survives reloads.
  const [selected, setSelected] = useLocalStorage<string[]>(
    "backtest.council",
    PERSONA_SLUGS,
  );
  const full = selected.length >= PERSONA_ORDER.length || selected.length === 0;
  const subsetKey = useDebounced(full ? "" : [...selected].sort().join(","), 250);
  const personaFetcher = useCallback(
    (s: AbortSignal) =>
      api.backtestPersonas(subsetKey ? subsetKey.split(",") : undefined, s),
    [subsetKey],
  );
  const {
    data: pdata,
    loading: ploading,
    error: perror,
  } = useAsync<PersonaStudy>(personaFetcher, [subsetKey]);

  const vintageFetcher = useCallback((s: AbortSignal) => api.backtestVintages({}, s), []);
  const {
    data: vdata,
    loading: vloading,
    error: verror,
  } = useAsync<VintageStudy>(vintageFetcher, []);

  const [metric, setMetric] = useState<Metric>("mean_excess");
  const [focusH, setFocusH] = useState<number>(21);
  // Execution-reality lenses on the grouped tables: show returns after trading
  // costs, and/or drop the thin (unrealizable-in-size) names.
  const [basis, setBasis] = useState<"gross" | "net">("gross");
  const [exThin, setExThin] = useState(false);

  const [wSort, setWSort] = useState<SortState>({
    key: "excess_to_date",
    order: "desc",
  });
  const [q, setQ] = useState("");

  const priced = useMemo(
    () => (data?.stocks ?? []).filter((s) => s.entry_price != null),
    [data],
  );

  const winnerRows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    let rows = priced;
    if (needle) {
      rows = rows.filter(
        (s) =>
          s.symbol.toLowerCase().includes(needle) ||
          (s.company_name ?? "").toLowerCase().includes(needle),
      );
    }
    const dir = wSort.order === "desc" ? -1 : 1;
    const key = wSort.key as keyof BacktestStock;
    return [...rows].sort((a, b) => {
      const va = a[key] as string | number | null | undefined;
      const vb = b[key] as string | number | null | undefined;
      if (va == null && vb == null) return 0;
      if (va == null) return 1; // nulls last, whatever the order
      if (vb == null) return -1;
      if (typeof va === "string" || typeof vb === "string")
        return String(va).localeCompare(String(vb)) * dir;
      return (va - (vb as number)) * dir;
    });
  }, [priced, q, wSort]);

  function handleWSort(key: string) {
    setWSort((s) =>
      s.key === key
        ? { key, order: s.order === "desc" ? "asc" : "desc" }
        : { key, order: key === "symbol" ? "asc" : "desc" },
    );
  }

  const togglePersona = useCallback(
    (slug: string) => {
      setSelected((prev) => {
        const base = prev.length === 0 ? PERSONA_SLUGS : prev;
        const next = new Set(base);
        if (next.has(slug)) next.delete(slug);
        else next.add(slug);
        return PERSONA_SLUGS.filter((s) => next.has(s));
      });
    },
    [setSelected],
  );

  const winnerColumns: Column<BacktestStock>[] = useMemo(
    () => [
      {
        key: "symbol",
        header: "Symbol",
        sortKey: "symbol",
        render: (r) => (
          <span className="flex items-center gap-1.5">
            <Link
              href={`/stock/${encodeURIComponent(r.symbol)}`}
              className="num font-medium text-paper hover:text-brass"
            >
              {r.symbol}
            </Link>
            {r.thin && (
              <span
                className="rounded-sm bg-brass/15 px-1 text-[9px] uppercase tracking-wide text-brass"
                title="Thin — trades below ₹25 lakh/day (median). Its return may be unrealizable in size."
              >
                thin
              </span>
            )}
          </span>
        ),
      },
      {
        key: "company",
        header: "Company",
        render: (r) => (
          <span className="serif block max-w-[220px] truncate text-[13px] text-paper/80">
            {r.company_name ?? DASH}
          </span>
        ),
      },
      {
        key: "composite",
        header: "Composite",
        sortKey: "composite",
        align: "right",
        title: "Average of the 10 persona scores, on a 0-100 scale.",
        render: (r) => (
          <span className="num text-xs" style={{ color: compositeColor(r.composite) }}>
            {composite(r.composite)}
          </span>
        ),
      },
      {
        key: "lcb",
        header: "Conf-adj",
        sortKey: "lcb",
        align: "right",
        title:
          "Confidence-adjusted score: the composite minus a penalty for how much the personas disagree. Our conservative ranking key.",
        render: (r) => (
          <span className="num text-xs" style={{ color: compositeColor(r.lcb) }}>
            {composite(r.lcb)}
          </span>
        ),
      },
      {
        key: "tier",
        header: "Confidence",
        render: (r) => <TierChip tier={r.tier} size="xs" />,
      },
      {
        key: "rec",
        header: "Call",
        render: (r) => <RecChip rec={r.recommendation} size="xs" />,
      },
      {
        key: "entry",
        header: "Entry",
        sortKey: "entry_date",
        align: "right",
        title:
          "The next session's open after the view formed (no lookahead); the price you could actually have bought at.",
        render: (r) => (
          <span
            className="num whitespace-nowrap text-xs text-muted"
            title={
              r.entry_delay_days > 0
                ? `Entry delayed ${r.entry_delay_days} trading ${r.entry_delay_days === 1 ? "day" : "days"} past circuit locks. Basis: ${r.entry_basis ?? "—"}.`
                : r.entry_basis
                  ? `Filled at the ${r.entry_basis}.`
                  : undefined
            }
          >
            {shortDate(r.entry_date)}
            {r.entry_delay_days > 0 && (
              <span className="text-brass/70"> +{r.entry_delay_days}d</span>
            )}
          </span>
        ),
      },
      {
        key: "ret",
        header: "Return",
        sortKey: "return_to_date",
        align: "right",
        title: "Price change from entry to the latest close.",
        render: (r) => (
          <span className="num text-xs" style={{ color: signColor(r.return_to_date) }}>
            {pct(r.return_to_date)}
          </span>
        ),
      },
      {
        key: "excess",
        header: "Excess vs bench",
        sortKey: "excess_to_date",
        align: "right",
        title: "Return minus the benchmark's return over the same window.",
        render: (r) => (
          <span className="num text-xs" style={{ color: signColor(r.excess_to_date) }}>
            {pct(r.excess_to_date)}
          </span>
        ),
      },
    ],
    [],
  );

  if (error) {
    return (
      <Panel>
        <ErrorState message={error} onRetry={refetch} />
      </Panel>
    );
  }

  if (loading && !data) {
    return (
      <div className="space-y-5">
        <Skeleton style={{ width: 180, height: 24 }} />
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} style={{ height: 82 }} />
          ))}
        </div>
        <Panel>
          <div className="p-4">
            <Skeleton style={{ height: 300 }} />
          </div>
        </Panel>
        <Panel>
          <TableSkeleton rows={8} />
        </Panel>
      </div>
    );
  }

  if (!data) return null;

  const unpriced = data.cohort_size - data.priced;
  const hit21 = data.overall.hit_rate?.["21"] ?? null;
  const hit21ci = data.overall.hit_rate_ci?.["21"] ?? null;
  const hit21Verdict = hit21ci
    ? hit21ci.verdict === "positive"
      ? "beats a coin flip"
      : hit21ci.verdict === "negative"
        ? "worse than a coin flip"
        : "like a coin flip"
    : null;
  // Sensitivity guard: the SAME hit rate with the presumed-delisted names folded
  // back in via the policy. When it exists, show it as the headline and the
  // measured-only number + delta live in the tooltip.
  const hit21Policy = data.overall_with_policy?.hit_rate?.["21"] ?? null;
  const hasPolicy = data.overall_with_policy != null;
  const hit21Shown = hasPolicy && hit21Policy != null ? hit21Policy : hit21;
  const hit21Delta =
    hasPolicy && hit21Policy != null && hit21 != null
      ? hit21Policy - hit21
      : null;
  const availableHorizons = data.horizons.filter(
    (h) => data.overall.mean_excess?.[String(h)] != null,
  );
  const unavailable = data.horizons.filter(
    (h) => !availableHorizons.includes(h),
  );
  const selectionLabel = full
    ? "the full council"
    : `${selected.length} selected ${selected.length === 1 ? "investor" : "investors"}`;

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="serif text-2xl font-semibold text-paper">Backtest</h1>
        <span className="num text-xs text-muted">
          did the scores pick winners? · vs{" "}
          <span className="text-paper">{data.benchmark}</span> (Nifty 500)
        </span>
      </div>

      {/* cohort completeness — how much of the scored universe we actually saw */}
      <CohortSummary cohort={data.cohort} policy={data.delisting_return_policy} />

      {/* honesty note */}
      <div className="rounded-md border border-brass/30 bg-brass/5 px-4 py-2.5 text-xs leading-relaxed text-muted">
        <span className="font-semibold uppercase tracking-[0.12em] text-brass">
          Pilot window
        </span>{" "}
        — one cohort, ~2.5 months of forward data since 2026-04-20.
        {unavailable.length > 0 && (
          <>
            {" "}
            The{" "}
            {unavailable.map((h) => horizonLabel(h)).join(" / ")} horizons are
            not yet computable.
          </>
        )}{" "}
        These numbers are calibration data for the confidence layer, not
        validation of it.
      </div>

      {/* execution reality — the terms these returns are measured under */}
      <ExecutionNote study={data} />

      {/* header stats */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatCard
          label="Cohort"
          value={data.cohort_size.toLocaleString("en-IN")}
          sub="earliest scored view per stock"
        />
        <StatCard
          label="With prices"
          value={data.priced.toLocaleString("en-IN")}
          sub={
            unpriced > 0 ? (
              <span className="text-terracotta">
                {unpriced.toLocaleString("en-IN")} of{" "}
                {data.cohort_size.toLocaleString("en-IN")} have no price history
              </span>
            ) : (
              "full price coverage"
            )
          }
          accent={unpriced > 0 ? "var(--color-brass)" : "var(--color-sage)"}
        />
        <StatCard
          label="Benchmark"
          value={<span className="text-lg">{data.benchmark}</span>}
          sub={`Nifty 500 · ${data.benchmark_bars} bars`}
        />
        <StatCard
          label="Beat benchmark @ 1 month"
          value={hit21Shown == null ? DASH : `${hit21Shown.toFixed(0)}%`}
          sub={
            <span
              title={
                hasPolicy && hit21 != null && hit21Delta != null
                  ? `with delisting policy: ${hit21Shown!.toFixed(0)}% · measured-only: ${hit21.toFixed(0)}% · delta ${hit21Delta > 0 ? "+" : ""}${hit21Delta.toFixed(0)}pp${hit21ci ? ` · ${ciRange(hit21ci)}` : ""}`
                  : hit21ci
                    ? ciRange(hit21ci)
                    : undefined
              }
            >
              {hasPolicy ? "policy-adjusted, " : "of priced names, "}21 trading
              days on
              {hasPolicy && hit21Delta != null && (
                <>
                  {" · "}
                  <span style={{ color: signColor(hit21Delta) }}>
                    {hit21Delta > 0 ? "+" : ""}
                    {hit21Delta.toFixed(0)}pp vs measured-only
                  </span>
                </>
              )}
              {!hasPolicy && hit21Verdict && (
                <>
                  {" · "}
                  <span style={{ color: verdictColor(hit21ci!.verdict) }}>
                    {hit21Verdict}
                  </span>
                </>
              )}
            </span>
          }
          accent={hit21Shown == null ? undefined : hit21Shown >= 50 ? "var(--color-sage)" : "var(--color-terracotta)"}
        />
      </div>

      {/* ── CHARTS FIRST ─────────────────────────────────────────── */}

      {/* equity curve of the selected council + picker */}
      <Panel>
        <PanelHeader
          title="Growth of a BUY basket"
          editorial
          hint={
            <span title="An equal-weighted portfolio of the picks the selected council rates BUY, each entered the day after its view formed, rebased to 100.">
              {selectionLabel}, ₹100 at entry
            </span>
          }
        />
        <div className="grid grid-cols-1 gap-4 p-4 lg:grid-cols-[1fr_220px]">
          <div>
            {perror ? (
              <p className="py-16 text-center text-sm text-terracotta">{perror}</p>
            ) : !pdata ? (
              <Skeleton style={{ height: 300 }} />
            ) : (
              <EquityCurve
                points={pdata.subset.curve}
                benchmarkLabel={`${data.benchmark} benchmark`}
                loading={ploading}
              />
            )}
            {pdata && (
              <p className="mt-2 text-[11px] text-muted">
                {pdata.subset.n_buy} BUY {pdata.subset.n_buy === 1 ? "pick" : "picks"} in
                this selection ({pdata.subset.n_buy_priced} with prices) ·{" "}
                {pdata.subset.n_avoid} AVOID.
                {pdata.subset.n_thin > 0 && (
                  <span
                    title="Below the liquidity floor (₹25 lakh/day median traded value) — their contribution to this basket may be unrealizable in size."
                  >
                    {" "}
                    <span className="text-brass">{pdata.subset.n_thin}</span>{" "}
                    of these {pdata.subset.n_thin === 1 ? "is" : "are"} thin.
                  </span>
                )}{" "}
                The dashed line is the same basket after 85 bps/side trading
                costs. The basket can shrink at longer horizons as forward prices
                run out — hover any point for its size.
              </p>
            )}
          </div>
          <div className="lg:border-l lg:border-hairline lg:pl-4">
            <PersonaWeighting selected={selected} onChange={setSelected} />
          </div>
        </div>
      </Panel>

      {/* per-persona comparison */}
      <Panel>
        <PanelHeader
          title="Each investor's record"
          editorial
          right={
            <div className="flex flex-wrap items-center gap-2">
              <Toggle
                options={[
                  { key: "mean_excess", label: "Excess return" },
                  { key: "hit_rate", label: "Hit rate" },
                  { key: "spread", label: "BUY–AVOID edge" },
                ]}
                value={metric}
                onChange={(v) => setMetric(v as Metric)}
              />
              <Toggle
                options={data.horizons.map((h) => ({
                  key: String(h),
                  label: horizonLabel(h),
                  title: horizonTitle(h),
                }))}
                value={String(focusH)}
                onChange={(v) => setFocusH(Number(v))}
              />
            </div>
          }
        />
        <div className="p-4">
          {perror ? (
            <p className="py-8 text-center text-sm text-terracotta">{perror}</p>
          ) : !pdata ? (
            <TableSkeleton rows={10} />
          ) : (
            <PersonaRanking
              personas={pdata.personas}
              metric={metric}
              horizon={focusH}
              selected={full ? [] : selected}
              onToggle={togglePersona}
            />
          )}
        </div>
      </Panel>

      {/* signal over time — the study rolled forward, with era attribution */}
      {verror ? (
        <Panel>
          <PanelHeader title="Signal over time" editorial />
          <p className="px-4 py-8 text-center text-sm text-terracotta">{verror}</p>
        </Panel>
      ) : !vdata ? (
        <Panel>
          <div className="p-4">
            <Skeleton style={{ height: 280 }} />
          </div>
        </Panel>
      ) : (
        <div className={vloading ? "opacity-60 transition-opacity" : "transition-opacity"}>
          <VintageSignals data={vdata} benchmark={data.benchmark} />
        </div>
      )}

      {/* ── TABLES AFTER ─────────────────────────────────────────── */}

      {/* lens controls for the grouped tables: gross vs after-friction, and
          whether to drop thin (unrealizable-in-size) names from the headline */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-xs text-muted">
          Return basis for the grouped tables
        </span>
        <div className="flex flex-wrap items-center gap-2">
          <Toggle
            options={[
              { key: "gross", label: "Gross", title: "Before trading costs." },
              {
                key: "net",
                label: "After friction",
                title: "Net of 85 bps a side, charged on both the buy and the sell.",
              },
            ]}
            value={basis}
            onChange={(v) => setBasis(v as "gross" | "net")}
          />
          {data.overall_ex_thin && (
            <label
              className="flex cursor-pointer items-center gap-1.5 text-[11px] text-muted"
              title={`Drop names trading below ₹${(data.adv_floor / 100000).toFixed(0)} lakh/day (median) from the All-cohort row — their returns may be unrealizable in size.`}
            >
              <input
                type="checkbox"
                checked={exThin}
                onChange={(e) => setExThin(e.target.checked)}
                className="accent-brass"
              />
              Exclude thin picks
            </label>
          )}
        </div>
      </div>

      <BucketTable
        title="By confidence level"
        hint="excess return vs benchmark, per horizon"
        buckets={data.by_tier}
        order={TIER_ORDER}
        horizons={data.horizons}
        overall={exThin && data.overall_ex_thin ? data.overall_ex_thin : data.overall}
        overallLabel={exThin && data.overall_ex_thin ? "All cohort · ex-thin" : "All cohort"}
        labelFor={(k) => <TierChip tier={k} size="xs" />}
        basis={basis}
      />

      <div className="grid grid-cols-1 gap-5 xl:grid-cols-2">
        <BucketTable
          title="By confidence-adjusted score"
          hint="cohort split into fifths · 5 = highest scored"
          buckets={data.by_lcb_quintile}
          order={QUINTILE_ORDER}
          horizons={data.horizons}
          basis={basis}
          labelFor={(k) => (
            <span className="num text-paper" title={`Fifth ${k} of 5 (5 = highest)`}>
              Fifth {k}
            </span>
          )}
        />
        <BucketTable
          title="By composite score"
          hint="cohort split into fifths · 5 = highest scored"
          buckets={data.by_composite_quintile}
          order={QUINTILE_ORDER}
          horizons={data.horizons}
          basis={basis}
          labelFor={(k) => (
            <span className="num text-paper" title={`Fifth ${k} of 5 (5 = highest)`}>
              Fifth {k}
            </span>
          )}
        />
      </div>

      {/* rank-correlation panel (formerly "IC") */}
      <Panel>
        <PanelHeader
          title="Does a higher score predict a higher return?"
          hint="rank correlation, −1 to +1"
        />
        <div className="p-4">
          <p className="mb-3 text-xs text-muted">
            How closely the score order matches the return order across the
            cohort (Spearman rank correlation). +1 means higher scores lined up
            perfectly with higher returns, 0 means no relationship, −1 means the
            opposite. Reported at each horizon.
          </p>
          <div className="w-full overflow-x-auto">
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="border-b border-hairline text-[10px] uppercase tracking-wide text-muted">
                  <th className="px-3 py-2 text-left font-semibold">Score</th>
                  {data.horizons.map((h) => (
                    <th
                      key={h}
                      className="px-3 py-2 text-right font-semibold"
                      title={horizonTitle(h)}
                    >
                      {horizonLabel(h)}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {(["composite", "lcb"] as const).map((key) => (
                  <tr key={key} className="border-b border-hairline/60 last:border-0">
                    <td
                      className="px-3 py-2 text-xs text-paper/80"
                      title={
                        key === "lcb"
                          ? "Composite minus a disagreement penalty."
                          : "Average of the 10 persona scores."
                      }
                    >
                      {key === "lcb" ? "Confidence-adjusted" : "Composite"}
                    </td>
                    {data.horizons.map((h) => {
                      const rho = data.ic[key]?.[String(h)] ?? null;
                      const ci = data.ic_ci?.[key]?.[String(h)] ?? null;
                      return (
                        <td
                          key={h}
                          className="px-3 py-2 text-right align-top"
                          title={ci ? ciRange(ci) : undefined}
                        >
                          <div className="num text-xs" style={{ color: signColor(rho) }}>
                            {rho == null ? DASH : rho.toFixed(3)}
                          </div>
                          {ci && (
                            <div
                              className="text-[9px] uppercase tracking-wide"
                              style={{ color: verdictColor(ci.verdict) }}
                            >
                              {VERDICT_LABEL[ci.verdict]}
                            </div>
                          )}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </Panel>

      {/* winners / losers */}
      <Panel>
        <PanelHeader
          title="Every priced name, since entry"
          hint="the whole cohort with a price — sortable"
          right={
            <div className="flex items-center gap-3">
              <input
                type="search"
                value={q}
                onChange={(e) => setQ(e.target.value)}
                placeholder="Filter symbol or company…"
                aria-label="Filter backtest stocks"
                className="w-48 rounded-sm border border-hairline bg-panel px-2 py-1 text-xs text-paper outline-none placeholder:text-muted/70 focus:border-brass"
              />
              <span className="num text-xs text-muted">
                {winnerRows.length}/{priced.length}
              </span>
            </div>
          }
        />
        {winnerRows.length === 0 ? (
          <div className="px-4 py-8 text-center text-sm text-muted">
            No priced stocks match.
          </div>
        ) : (
          <DataTable<BacktestStock>
            columns={winnerColumns}
            rows={winnerRows}
            rowKey={(r) => r.symbol}
            sort={wSort}
            onSort={handleWSort}
          />
        )}
      </Panel>
    </div>
  );
}

// ── small segmented toggle (mirrors PriceChart) ───────────────────
function Toggle({
  options,
  value,
  onChange,
}: {
  options: { key: string; label: string; title?: string }[];
  value: string;
  onChange: (key: string) => void;
}) {
  return (
    <div className="inline-flex overflow-hidden rounded-sm border border-hairline">
      {options.map((o) => {
        const active = o.key === value;
        return (
          <button
            key={o.key}
            onClick={() => onChange(o.key)}
            aria-pressed={active}
            title={o.title}
            className={`px-2 py-1 text-[11px] font-medium transition-colors ${
              active ? "bg-brass/15 text-brass" : "text-muted hover:text-paper"
            }`}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

// ── execution-reality note ────────────────────────────────────────
// Plain-language statement of the terms the returns are measured under: how
// entries fill, what friction is charged, and where the liquidity floor sits.
// This is the honesty banner for B2 — the gross numbers elsewhere are academic
// without it.

function ExecutionNote({ study }: { study: BacktestStudy }) {
  const lakh = (study.adv_floor / 100000).toFixed(0);
  const unenterable = study.cohort.excluded.unenterable;
  const thin = study.cohort.thin;
  const measured = study.cohort.measured;
  return (
    <div className="rounded-md border border-hairline bg-panel/60 px-4 py-2.5 text-xs leading-relaxed text-muted">
      <span className="font-semibold uppercase tracking-[0.12em] text-paper/80">
        Execution reality
      </span>{" "}
      — entries fill at the{" "}
      <span
        className="text-paper/90"
        title="The signal forms on a close you can't transact at, so we buy at the next session's opening price (or that day's close when no open is recorded)."
      >
        next session&apos;s open
      </span>{" "}
      after the view forms, skipping{" "}
      <span
        className="text-paper/90"
        title={`A day pinned at a price limit (no intraday range, a full band from the prior close) can't be traded. Entry waits up to ${study.entry_window} trading days for a tradeable session.`}
      >
        circuit-locked days
      </span>
      {unenterable > 0 ? (
        <>
          {" "}
          (
          <span className="text-terracotta">
            {unenterable} never opened a window
          </span>
          )
        </>
      ) : null}
      . Returns are shown gross and{" "}
      <span
        className="text-paper/90"
        title={`${study.friction_bps} basis points one way — securities tax, exchange fees, and a spread/impact allowance for illiquid smallcaps — charged on both the buy and the sell.`}
      >
        after {study.friction_bps} bps/side friction
      </span>
      .{" "}
      {thin > 0 ? (
        <>
          <span className="text-brass">{thin}</span> of {measured} measured names
          trade below{" "}
          <span
            className="text-paper/90"
            title="Median daily traded value over the 21 sessions before entry. Below this, a position of any size may not be fillable at the printed price."
          >
            ₹{lakh} lakh/day
          </span>{" "}
          — flagged thin and reported separately.
        </>
      ) : (
        <>
          All measured names clear the ₹{lakh} lakh/day liquidity floor.
        </>
      )}
    </div>
  );
}

// ── cohort completeness banner ────────────────────────────────────
// "measured N of M scored (K excluded: …)" — prominent, with an expandable
// breakdown and a caution line when coverage is thin. Vanishing names are not
// neutral (delisted skews toward failures), so this refuses to hide them.

function CohortSummary({
  cohort,
  policy,
}: {
  cohort: CohortBlock;
  policy: number;
}) {
  const { scored, measured, excluded, presumed_outcomes: outcomes } = cohort;
  const totalExcluded =
    excluded.no_bars +
    excluded.unenterable +
    excluded.bars_predate_view +
    excluded.insufficient_forward;
  const ratio = scored > 0 ? measured / scored : 1;
  const partial = ratio < 0.8;
  const pol = cohort.policy;
  const policyApplied = pol.applied_constant + pol.applied_actual_last;

  const reasonBits = [
    excluded.no_bars > 0 ? `${excluded.no_bars} no price bars` : null,
    excluded.unenterable > 0
      ? `${excluded.unenterable} circuit-locked at entry`
      : null,
    excluded.bars_predate_view > 0
      ? `${excluded.bars_predate_view} bars predate the view`
      : null,
    excluded.insufficient_forward > 0
      ? `${excluded.insufficient_forward} too little forward data`
      : null,
  ].filter(Boolean);

  return (
    <details className="group rounded-md border border-hairline bg-panel/60 px-4 py-3">
      <summary className="flex cursor-pointer list-none flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="text-sm text-paper">
          Measured{" "}
          <span
            className="num font-semibold"
            style={{ color: partial ? "var(--color-terracotta)" : "var(--color-sage)" }}
          >
            {measured.toLocaleString("en-IN")}
          </span>{" "}
          of{" "}
          <span className="num font-semibold text-paper">
            {scored.toLocaleString("en-IN")}
          </span>{" "}
          scored
        </span>
        {totalExcluded > 0 && (
          <span className="num text-xs text-muted">
            ({totalExcluded.toLocaleString("en-IN")} excluded:{" "}
            {reasonBits.join(" · ")})
          </span>
        )}
        <span className="ml-auto text-[11px] text-brass group-open:hidden">
          breakdown ▾
        </span>
        <span className="ml-auto hidden text-[11px] text-brass group-open:inline">
          hide ▴
        </span>
      </summary>

      {partial && (
        <p className="mt-2 text-xs font-medium text-terracotta">
          Results describe a partial cohort ({(ratio * 100).toFixed(0)}% of scored
          names measured) — treat with caution.
        </p>
      )}

      <div className="mt-3 grid grid-cols-1 gap-3 text-xs text-muted sm:grid-cols-3">
        <div>
          <div className="mb-1 text-[10px] uppercase tracking-wide text-muted/80">
            Excluded, by reason
          </div>
          <Row label="No price bars" value={excluded.no_bars} />
          <Row label="Circuit-locked at entry" value={excluded.unenterable} />
          <Row label="Bars predate the view" value={excluded.bars_predate_view} />
          <Row
            label="Too little forward data"
            value={excluded.insufficient_forward}
          />
          {cohort.thin > 0 && (
            <div
              className="mt-1 border-t border-hairline/50 pt-1"
              title="Not excluded — measured, but below the liquidity floor. Their returns may be unrealizable in size, so headline stats are also published ex-thin."
            >
              <Row label="Thin (below ADV floor)" value={cohort.thin} />
            </div>
          )}
        </div>
        <div>
          <div className="mb-1 text-[10px] uppercase tracking-wide text-muted/80">
            Presumed fate of the vanished
          </div>
          <Row label="Delisted" value={outcomes.delisted} />
          <Row label="Merged" value={outcomes.merged} />
          <Row label="Unknown / data gap" value={outcomes.unknown} />
        </div>
        <div>
          <div className="mb-1 text-[10px] uppercase tracking-wide text-muted/80">
            Delisting return policy
          </div>
          <p className="leading-relaxed">
            Presumed-delisted names are folded back in at a documented{" "}
            <span className="num text-paper">{policy.toFixed(0)}%</span> loss
            (merged / unknown stay excluded, no synthetic return).
          </p>
          <Row label="Assigned the constant" value={pol.applied_constant} />
          <Row
            label="Used actual last price"
            value={pol.applied_actual_last}
          />
          {policyApplied === 0 && (
            <p className="mt-1 text-[11px] text-muted/80">
              No policy return applied to this cohort.
            </p>
          )}
        </div>
      </div>
    </details>
  );
}

function Row({ label, value }: { label: string; value: number }) {
  return (
    <div className="flex items-baseline justify-between gap-2 py-0.5">
      <span>{label}</span>
      <span className="num text-paper/80">{value.toLocaleString("en-IN")}</span>
    </div>
  );
}

// ── grouped bucket table ──────────────────────────────────────────

function BucketTable({
  title,
  hint,
  buckets,
  order,
  horizons,
  overall,
  labelFor,
  basis = "gross",
  overallLabel = "All cohort",
}: {
  title: string;
  hint?: string;
  buckets: Record<string, BacktestBucket>;
  order: string[];
  horizons: number[];
  overall?: BacktestBucket;
  labelFor?: (key: string) => ReactNode;
  basis?: "gross" | "net";
  overallLabel?: string;
}) {
  const net = basis === "net";
  // Known keys in canonical order first, then anything unexpected.
  const keys = [
    ...order.filter((k) => buckets[k]),
    ...Object.keys(buckets)
      .filter((k) => !order.includes(k))
      .sort(),
  ];
  return (
    <Panel>
      <PanelHeader
        title={title}
        hint={hint}
        right={
          <span
            className="num text-[10px] text-muted"
            title={
              net
                ? "Each cell shows: mean excess return AFTER trading costs (85 bps a side) / median gross excess · share of names that beat the benchmark. Hover a cell for the gross figure."
                : "Each cell shows: mean excess return / median excess return · share of names that beat the benchmark. Hover a cell for the after-friction figure."
            }
          >
            {net ? "net mean" : "mean"} / median excess · hit rate
          </span>
        }
      />
      <div className="w-full overflow-x-auto">
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="border-b border-hairline text-[10px] uppercase tracking-wide text-muted">
              <th className="px-3 py-2 text-left font-semibold">Group</th>
              <th
                className="px-3 py-2 text-right font-semibold"
                title="Names with a price / total in the group."
              >
                Priced / total
              </th>
              {horizons.map((h) => (
                <th
                  key={h}
                  className="px-3 py-2 text-right font-semibold"
                  title={horizonTitle(h)}
                >
                  {horizonLabel(h)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {keys.map((k) => (
              <BucketRow
                key={k}
                label={labelFor ? labelFor(k) : k}
                bucket={buckets[k]}
                horizons={horizons}
                net={net}
              />
            ))}
            {overall && (
              <BucketRow
                label={
                  <span className="text-xs uppercase tracking-wide text-muted">
                    {overallLabel}
                  </span>
                }
                bucket={overall}
                horizons={horizons}
                net={net}
              />
            )}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}

function BucketRow({
  label,
  bucket,
  horizons,
  net = false,
}: {
  label: ReactNode;
  bucket: BacktestBucket;
  horizons: number[];
  net?: boolean;
}) {
  return (
    <tr className="border-b border-hairline/60 last:border-0">
      <td className="px-3 py-2">{label}</td>
      <td
        className="num px-3 py-2 text-right text-xs text-muted"
        title={bucket.thin > 0 ? `${bucket.thin} thin (below the liquidity floor)` : undefined}
      >
        <span className="text-paper">{bucket.priced}</span>/{bucket.n}
        {bucket.thin > 0 && (
          <span className="text-brass/70"> · {bucket.thin} thin</span>
        )}
      </td>
      {horizons.map((h) => {
        const k = String(h);
        const gross = bucket.mean_excess?.[k] ?? null;
        const netMean = bucket.net_mean_excess?.[k] ?? null;
        const shown = net ? netMean : gross;
        const med = bucket.median_excess?.[k] ?? null;
        const hit = bucket.hit_rate?.[k] ?? null;
        const meanCi = net ? bucket.net_mean_excess_ci?.[k] ?? null : bucket.mean_excess_ci?.[k] ?? null;
        if (shown == null && med == null && hit == null) {
          return (
            <td key={h} className="num px-3 py-2 text-right text-xs text-muted">
              {DASH}
            </td>
          );
        }
        return (
          <td
            key={h}
            className="num whitespace-nowrap px-3 py-2 text-right text-xs"
            title={`gross mean excess ${pct(gross)} · after friction ${pct(netMean)} · median ${med == null ? DASH : pct(med)} · ${hit == null ? DASH : `${hit.toFixed(0)}% beat benchmark`}${meanCi ? ` · ${ciRange(meanCi)}` : ""}`}
          >
            <span style={{ color: isNoise(meanCi) ? "var(--color-muted)" : signColor(shown) }}>
              {pct(shown)}
            </span>
            <span className="text-muted"> / {med == null ? DASH : pct(med)}</span>
            <span className="text-muted"> · </span>
            <span className="text-paper/80">
              {hit == null ? DASH : `${hit.toFixed(0)}%`}
            </span>
          </td>
        );
      })}
    </tr>
  );
}
