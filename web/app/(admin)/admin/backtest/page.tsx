"use client";

import Link from "next/link";
import { useCallback, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { api } from "@/lib/api";
import { useAsync } from "@/lib/hooks";
import type { BacktestBucket, BacktestStock, BacktestStudy } from "@/lib/types";
import { composite, compositeColor, pct, shortDate, DASH } from "@/lib/format";
import StatCard from "@/components/StatCard";
import DataTable, { Column, SortState } from "@/components/DataTable";
import RecChip from "@/components/RecChip";
import TierChip from "@/components/TierChip";
import { Panel, PanelHeader } from "@/components/Panel";
import { ErrorState, Skeleton, TableSkeleton } from "@/components/States";

const TIER_ORDER = ["high", "moderate", "mixed", "provisional"];
const QUINTILE_ORDER = ["5", "4", "3", "2", "1"];

function signColor(v: number | null | undefined): string {
  if (v == null) return "var(--color-muted)";
  return v >= 0 ? "var(--color-sage)" : "var(--color-terracotta)";
}

export default function BacktestPage() {
  const fetcher = useCallback((s: AbortSignal) => api.backtest(s), []);
  const { data, loading, error, refetch } = useAsync<BacktestStudy>(fetcher, []);

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

  const winnerColumns: Column<BacktestStock>[] = useMemo(
    () => [
      {
        key: "symbol",
        header: "Symbol",
        sortKey: "symbol",
        render: (r) => (
          <Link
            href={`/stock/${encodeURIComponent(r.symbol)}`}
            className="num font-medium text-paper hover:text-brass"
          >
            {r.symbol}
          </Link>
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
        render: (r) => (
          <span className="num text-xs" style={{ color: compositeColor(r.composite) }}>
            {composite(r.composite)}
          </span>
        ),
      },
      {
        key: "lcb",
        header: "LCB",
        sortKey: "lcb",
        align: "right",
        render: (r) => (
          <span className="num text-xs" style={{ color: compositeColor(r.lcb) }}>
            {composite(r.lcb)}
          </span>
        ),
      },
      {
        key: "tier",
        header: "Tier",
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
        render: (r) => (
          <span className="num whitespace-nowrap text-xs text-muted">
            {shortDate(r.entry_date)}
          </span>
        ),
      },
      {
        key: "ret",
        header: "Return",
        sortKey: "return_to_date",
        align: "right",
        render: (r) => (
          <span className="num text-xs" style={{ color: signColor(r.return_to_date) }}>
            {pct(r.return_to_date)}
          </span>
        ),
      },
      {
        key: "excess",
        header: "Excess",
        sortKey: "excess_to_date",
        align: "right",
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
          <TableSkeleton rows={8} />
        </Panel>
      </div>
    );
  }

  if (!data) return null;

  const unpriced = data.cohort_size - data.priced;
  const hit21 = data.overall.hit_rate?.["21"] ?? null;
  const availableHorizons = data.horizons.filter(
    (h) => data.overall.mean_excess?.[String(h)] != null,
  );
  const unavailable = data.horizons.filter(
    (h) => !availableHorizons.includes(h),
  );

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="serif text-2xl font-semibold text-paper">Backtest</h1>
        <span className="num text-xs text-muted">
          point-in-time event study · vs{" "}
          <span className="text-paper">{data.benchmark}</span>
        </span>
      </div>

      {/* honesty note */}
      <div className="rounded-md border border-brass/30 bg-brass/5 px-4 py-2.5 text-xs leading-relaxed text-muted">
        <span className="font-semibold uppercase tracking-[0.12em] text-brass">
          Pilot window
        </span>{" "}
        — one cohort, ~2.5 months of forward data since 2026-04-20.
        {unavailable.length > 0 && (
          <> The {unavailable.map((h) => `${h}d`).join(" / ")} horizons are not yet computable.</>
        )}{" "}
        These numbers are calibration data for the confidence layer, not
        validation of it.
      </div>

      {/* header stats */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatCard
          label="Cohort"
          value={data.cohort_size.toLocaleString("en-IN")}
          sub="earliest scored view per stock"
        />
        <StatCard
          label="Priced"
          value={data.priced.toLocaleString("en-IN")}
          sub={
            unpriced > 0 ? (
              <span className="text-terracotta">
                {unpriced.toLocaleString("en-IN")} of{" "}
                {data.cohort_size.toLocaleString("en-IN")} unpriced — coverage gap
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
          label="Hit rate @21d"
          value={hit21 == null ? DASH : `${hit21.toFixed(0)}%`}
          sub="of priced names beat the benchmark"
          accent={hit21 == null ? undefined : hit21 >= 50 ? "var(--color-sage)" : "var(--color-terracotta)"}
        />
      </div>

      {/* grouped excess-return tables */}
      <BucketTable
        title="By confidence tier"
        hint="excess vs benchmark per horizon"
        buckets={data.by_tier}
        order={TIER_ORDER}
        horizons={data.horizons}
        overall={data.overall}
        labelFor={(k) => <TierChip tier={k} size="xs" />}
      />

      <div className="grid grid-cols-1 gap-5 xl:grid-cols-2">
        <BucketTable
          title="By LCB quintile"
          hint="5 = best"
          buckets={data.by_lcb_quintile}
          order={QUINTILE_ORDER}
          horizons={data.horizons}
          labelFor={(k) => <span className="num text-paper">Q{k}</span>}
        />
        <BucketTable
          title="By composite quintile"
          hint="5 = best"
          buckets={data.by_composite_quintile}
          order={QUINTILE_ORDER}
          horizons={data.horizons}
          labelFor={(k) => <span className="num text-paper">Q{k}</span>}
        />
      </div>

      {/* IC panel */}
      <Panel>
        <PanelHeader
          title="Information coefficient"
          hint="Spearman rank IC per horizon, -1..1"
        />
        <div className="p-4">
          <p className="mb-3 text-xs text-muted">
            Rank correlation between score and forward return; &gt;0 means
            higher scores → higher returns.
          </p>
          <div className="w-full overflow-x-auto">
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="border-b border-hairline text-[10px] uppercase tracking-wide text-muted">
                  <th className="px-3 py-2 text-left font-semibold">Signal</th>
                  {data.horizons.map((h) => (
                    <th key={h} className="px-3 py-2 text-right font-semibold">
                      {h}d
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {(["composite", "lcb"] as const).map((key) => (
                  <tr key={key} className="border-b border-hairline/60 last:border-0">
                    <td className="px-3 py-2 text-xs uppercase tracking-wide text-paper/80">
                      {key === "lcb" ? "LCB" : "Composite"}
                    </td>
                    {data.horizons.map((h) => {
                      const rho = data.ic[key]?.[String(h)] ?? null;
                      return (
                        <td
                          key={h}
                          className="num px-3 py-2 text-right text-xs"
                          style={{ color: signColor(rho) }}
                        >
                          {rho == null ? DASH : rho.toFixed(3)}
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
          title="Winners & losers"
          hint="all priced cohort members, since entry"
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

// ── grouped bucket table ──────────────────────────────────────────

function BucketTable({
  title,
  hint,
  buckets,
  order,
  horizons,
  overall,
  labelFor,
}: {
  title: string;
  hint?: string;
  buckets: Record<string, BacktestBucket>;
  order: string[];
  horizons: number[];
  overall?: BacktestBucket;
  labelFor?: (key: string) => ReactNode;
}) {
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
          <span className="num text-[10px] text-muted">
            mean / median excess · hit rate
          </span>
        }
      />
      <div className="w-full overflow-x-auto">
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="border-b border-hairline text-[10px] uppercase tracking-wide text-muted">
              <th className="px-3 py-2 text-left font-semibold">Group</th>
              <th className="px-3 py-2 text-right font-semibold">Priced/N</th>
              {horizons.map((h) => (
                <th key={h} className="px-3 py-2 text-right font-semibold">
                  {h}d
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
              />
            ))}
            {overall && (
              <BucketRow
                label={
                  <span className="text-xs uppercase tracking-wide text-muted">
                    All cohort
                  </span>
                }
                bucket={overall}
                horizons={horizons}
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
}: {
  label: ReactNode;
  bucket: BacktestBucket;
  horizons: number[];
}) {
  return (
    <tr className="border-b border-hairline/60 last:border-0">
      <td className="px-3 py-2">{label}</td>
      <td className="num px-3 py-2 text-right text-xs text-muted">
        <span className="text-paper">{bucket.priced}</span>/{bucket.n}
      </td>
      {horizons.map((h) => {
        const k = String(h);
        const mean = bucket.mean_excess?.[k] ?? null;
        const med = bucket.median_excess?.[k] ?? null;
        const hit = bucket.hit_rate?.[k] ?? null;
        if (mean == null && med == null && hit == null) {
          return (
            <td key={h} className="num px-3 py-2 text-right text-xs text-muted">
              {DASH}
            </td>
          );
        }
        return (
          <td key={h} className="num whitespace-nowrap px-3 py-2 text-right text-xs">
            <span style={{ color: signColor(mean) }}>{pct(mean)}</span>
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
