"use client";

import Link from "next/link";
import { useCallback } from "react";
import { api } from "@/lib/api";
import { useAsync } from "@/lib/hooks";
import type {
  DataQualityOverview,
  ReconciliationOverview,
} from "@/lib/types";
import { relTime, titleCase } from "@/lib/format";
import { Panel, PanelHeader } from "@/components/Panel";
import { Meter } from "@/components/Bars";
import { ErrorState, TableSkeleton } from "@/components/States";

const GRADE_COLOR: Record<string, string> = {
  A: "var(--color-sage)",
  B: "var(--color-sage)",
  C: "var(--color-brass)",
  D: "var(--color-terracotta)",
  F: "var(--color-terracotta)",
  "?": "var(--color-muted)",
};

const DIM_ORDER = [
  "identity",
  "yfinance",
  "screener",
  "prices",
  "quarters",
  "freshness",
  "correctness",
];

function gradeColor(g: string | null) {
  return GRADE_COLOR[g ?? "?"] ?? "var(--color-muted)";
}

function coverageColor(pct: number | null) {
  if (pct == null) return "var(--color-muted)";
  if (pct >= 85) return "var(--color-sage)";
  if (pct >= 55) return "var(--color-brass)";
  return "var(--color-terracotta)";
}

function trustColor(rate: number | null) {
  if (rate == null) return "var(--color-muted)";
  if (rate >= 0.95) return "var(--color-sage)";
  if (rate >= 0.8) return "var(--color-brass)";
  return "var(--color-terracotta)";
}

// price / market_cap diverge in relative %, promoter_pct in absolute points.
function divergenceLabel(fact: string, value: number | null) {
  if (value == null) return "—";
  return fact === "promoter_pct"
    ? `${value.toFixed(2)}pts`
    : `${value.toFixed(2)}%`;
}

function fmtValue(v: number | null) {
  if (v == null) return "—";
  const abs = Math.abs(v);
  if (abs >= 1e7) return v.toExponential(2);
  return v.toLocaleString("en-IN", { maximumFractionDigits: 2 });
}

export default function DataQualityPage() {
  const fetcher = useCallback((s: AbortSignal) => api.adminDataQuality(s), []);
  const { data, loading, error, refetch } =
    useAsync<DataQualityOverview>(fetcher, []);

  const reconFetcher = useCallback(
    (s: AbortSignal) => api.adminReconciliation(s),
    [],
  );
  const { data: recon } = useAsync<ReconciliationOverview>(reconFetcher, []);

  const grades = data?.grades ?? {};
  const gradeTotal = Object.values(grades).reduce((a, b) => a + b, 0) || 1;

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="serif text-2xl font-semibold text-paper">Data Quality</h1>
        {data && (
          <span className="num text-xs text-muted">
            avg{" "}
            <span className="text-paper">{data.avg_overall ?? "—"}</span>/100 ·{" "}
            <span className="text-paper">{data.audited.toLocaleString("en-IN")}</span>{" "}
            audited
            {data.audited_at && <> · {relTime(data.audited_at)}</>}
          </span>
        )}
      </div>

      {error ? (
        <Panel>
          <ErrorState message={error} onRetry={refetch} />
        </Panel>
      ) : loading && !data ? (
        <Panel>
          <TableSkeleton rows={8} />
        </Panel>
      ) : !data || data.audited === 0 ? (
        <Panel>
          <div className="px-4 py-10 text-center text-sm text-muted">
            No data-quality audit has run yet. Launch{" "}
            <Link href="/admin/jobs" className="text-brass hover:underline">
              DQ Audit
            </Link>{" "}
            from the Jobs page.
          </div>
        </Panel>
      ) : (
        <>
          {/* Grade distribution */}
          <Panel>
            <PanelHeader title="Grade distribution" hint="latest audit, per stock" />
            <div className="p-4">
              <div className="flex h-6 w-full overflow-hidden rounded-sm bg-hairline/40">
                {["A", "B", "C", "D", "F", "?"].map((g) =>
                  grades[g] ? (
                    <div
                      key={g}
                      style={{
                        width: `${(grades[g] / gradeTotal) * 100}%`,
                        background: gradeColor(g),
                      }}
                      title={`${g}: ${grades[g]}`}
                    />
                  ) : null,
                )}
              </div>
              <div className="num mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
                {["A", "B", "C", "D", "F", "?"].map((g) =>
                  grades[g] ? (
                    <span key={g}>
                      <span style={{ color: gradeColor(g) }}>●</span> {g}{" "}
                      <span className="text-paper">{grades[g]}</span>
                    </span>
                  ) : null,
                )}
              </div>
            </div>
          </Panel>

          {/* Per-dimension coverage */}
          <Panel>
            <PanelHeader
              title="Dimension coverage"
              hint="% of scored stocks passing each dimension"
            />
            <div className="space-y-2.5 p-4">
              {DIM_ORDER.map((dim) => {
                const c = data.dimension_coverage[dim];
                if (!c) return null;
                return (
                  <div key={dim} className="flex items-center gap-3">
                    <span className="w-24 shrink-0 text-xs text-paper">
                      {titleCase(dim)}
                    </span>
                    <div className="flex-1">
                      <Meter
                        value={c.pct_pass ?? 0}
                        max={100}
                        color={coverageColor(c.pct_pass)}
                      />
                    </div>
                    <span className="num w-28 shrink-0 text-right text-[11px] text-muted">
                      {c.pct_pass ?? "—"}% pass ·{" "}
                      <span className="text-paper/70">{c.scored}</span>
                    </span>
                  </div>
                );
              })}
            </div>
          </Panel>

          {/* Gaps + flags rollup */}
          <div className="grid grid-cols-1 gap-5 md:grid-cols-2">
            <Panel>
              <PanelHeader title="Fillable gaps" hint="drives DQ Fill" />
              <TallyList tally={data.missing} empty="No gaps 🎉" />
            </Panel>
            <Panel>
              <PanelHeader title="Flags raised" hint="quality + correctness" />
              <TallyList tally={data.flags} empty="No flags" />
            </Panel>
          </div>

          {/* Cross-source discrepancies */}
          <Panel>
            <PanelHeader
              title="Cross-source discrepancies"
              hint="screener vs yfinance beyond tolerance — flagged, never overwritten"
              right={
                <span className="num text-xs text-muted">
                  {data.discrepancies.length} flagged
                </span>
              }
            />
            {data.discrepancies.length === 0 ? (
              <div className="px-4 py-6 text-center text-sm text-muted">
                No value discrepancies — sources agree.
              </div>
            ) : (
              <div className="w-full overflow-x-auto">
                <table className="w-full border-collapse text-sm">
                  <thead>
                    <tr className="border-b border-hairline text-[10px] uppercase tracking-wide text-muted">
                      <th className="px-3 py-2 text-left font-semibold">Symbol</th>
                      <th className="px-3 py-2 text-left font-semibold">Metric</th>
                      <th className="px-3 py-2 text-left font-semibold">Detail</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.discrepancies.map((d, i) => (
                      <tr
                        key={i}
                        className="border-b border-hairline/60 last:border-0"
                      >
                        <td className="px-3 py-1.5">
                          <Link
                            href={`/stock/${encodeURIComponent(d.symbol)}`}
                            className="num text-paper hover:text-brass"
                          >
                            {d.symbol}
                          </Link>
                        </td>
                        <td className="px-3 py-1.5 text-xs text-terracotta">
                          {d.type.replace("_mismatch", "")}
                        </td>
                        <td className="num px-3 py-1.5 text-xs text-muted">
                          {d.detail}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>

          {/* Worst offenders */}
          <Panel>
            <PanelHeader
              title="Lowest-quality stocks"
              hint="fix these first"
            />
            <div className="w-full overflow-x-auto">
              <table className="w-full border-collapse text-sm">
                <thead>
                  <tr className="border-b border-hairline text-[10px] uppercase tracking-wide text-muted">
                    <th className="px-3 py-2 text-left font-semibold">Symbol</th>
                    <th className="px-3 py-2 text-left font-semibold">Company</th>
                    <th className="px-3 py-2 text-right font-semibold">Score</th>
                    <th className="px-3 py-2 text-center font-semibold">Grade</th>
                    <th className="px-3 py-2 text-left font-semibold">Missing</th>
                  </tr>
                </thead>
                <tbody>
                  {data.worst.map((r) => (
                    <tr
                      key={r.symbol}
                      className="border-b border-hairline/60 last:border-0"
                    >
                      <td className="px-3 py-1.5">
                        <Link
                          href={`/stock/${encodeURIComponent(r.symbol)}`}
                          className="num text-paper hover:text-brass"
                        >
                          {r.symbol}
                        </Link>
                      </td>
                      <td className="serif max-w-[200px] truncate px-3 py-1.5 text-[13px] text-paper/80">
                        {r.company_name}
                      </td>
                      <td className="num px-3 py-1.5 text-right text-paper">
                        {r.overall_score ?? "—"}
                      </td>
                      <td className="px-3 py-1.5 text-center">
                        <span
                          className="num text-xs font-semibold"
                          style={{ color: gradeColor(r.grade) }}
                        >
                          {r.grade ?? "—"}
                        </span>
                      </td>
                      <td className="px-3 py-1.5">
                        <div className="flex flex-wrap gap-1">
                          {r.missing.slice(0, 5).map((m) => (
                            <span
                              key={m}
                              className="rounded-sm border border-hairline px-1 py-0.5 text-[9px] text-muted"
                            >
                              {m}
                            </span>
                          ))}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Panel>
        </>
      )}

      {recon && <ReconciliationSection recon={recon} />}
    </div>
  );
}

function ReconciliationSection({ recon }: { recon: ReconciliationOverview }) {
  return (
    <>
      {/* Per-source-per-fact trust */}
      <Panel>
        <PanelHeader
          title="Cross-source trust"
          hint="rolling 90d agreement rate, per source × fact"
          right={
            recon.last_run_at ? (
              <span className="num text-xs text-muted">
                {recon.compared.toLocaleString("en-IN")} compared ·{" "}
                {recon.stale.toLocaleString("en-IN")} stale · {relTime(recon.last_run_at)}
              </span>
            ) : undefined
          }
        />
        {recon.trust.length === 0 ? (
          <div className="px-4 py-6 text-center text-sm text-muted">
            No reconciliation has run yet.
          </div>
        ) : (
          <div className="w-full overflow-x-auto">
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="border-b border-hairline text-[10px] uppercase tracking-wide text-muted">
                  <th className="px-3 py-2 text-left font-semibold">Fact</th>
                  <th className="px-3 py-2 text-left font-semibold">Source</th>
                  <th className="px-3 py-2 text-right font-semibold">Agreement</th>
                  <th className="px-3 py-2 text-right font-semibold">Comparisons</th>
                </tr>
              </thead>
              <tbody>
                {recon.trust.map((t) => (
                  <tr
                    key={`${t.fact}:${t.source}`}
                    className="border-b border-hairline/60 last:border-0"
                  >
                    <td className="px-3 py-1.5 text-xs text-paper">
                      {titleCase(t.fact.replace(/_/g, " "))}
                    </td>
                    <td className="num px-3 py-1.5 text-xs text-paper/80">
                      {t.source}
                    </td>
                    <td
                      className="num px-3 py-1.5 text-right font-semibold"
                      style={{ color: trustColor(t.agreement_rate) }}
                    >
                      {t.agreement_rate == null
                        ? "—"
                        : `${(t.agreement_rate * 100).toFixed(1)}%`}
                    </td>
                    <td className="num px-3 py-1.5 text-right text-muted">
                      {t.agreements.toLocaleString("en-IN")}/
                      {t.comparisons.toLocaleString("en-IN")}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      {/* Open discrepancies */}
      <Panel>
        <PanelHeader
          title="Open discrepancies"
          hint="same fact, two sources, beyond tolerance — flagged, never overwritten"
          right={
            <span className="num text-xs text-muted">
              {recon.open_count.toLocaleString("en-IN")} open
            </span>
          }
        />
        {recon.open.length === 0 ? (
          <div className="px-4 py-6 text-center text-sm text-muted">
            No open discrepancies — sources agree.
          </div>
        ) : (
          <div className="w-full overflow-x-auto">
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="border-b border-hairline text-[10px] uppercase tracking-wide text-muted">
                  <th className="px-3 py-2 text-left font-semibold">Symbol</th>
                  <th className="px-3 py-2 text-left font-semibold">Fact</th>
                  <th className="px-3 py-2 text-left font-semibold">A vs B</th>
                  <th className="px-3 py-2 text-right font-semibold">Divergence</th>
                  <th className="px-3 py-2 text-right font-semibold">Detected</th>
                </tr>
              </thead>
              <tbody>
                {recon.open.map((d, i) => (
                  <tr
                    key={`${d.symbol}:${d.fact}:${d.fact_key}:${i}`}
                    className="border-b border-hairline/60 last:border-0"
                  >
                    <td className="px-3 py-1.5">
                      <Link
                        href={`/stock/${encodeURIComponent(d.symbol)}`}
                        className="num text-paper hover:text-brass"
                      >
                        {d.symbol}
                      </Link>
                    </td>
                    <td className="px-3 py-1.5 text-xs text-paper/80">
                      {titleCase(d.fact.replace(/_/g, " "))}
                      <span className="text-muted"> · {d.fact_key}</span>
                    </td>
                    <td className="num px-3 py-1.5 text-xs text-muted">
                      {d.source_a} {fmtValue(d.value_a)}
                      <span className="text-paper/40"> vs </span>
                      {d.source_b} {fmtValue(d.value_b)}
                    </td>
                    <td className="num px-3 py-1.5 text-right text-xs text-terracotta">
                      {divergenceLabel(d.fact, d.divergence_pct)}
                    </td>
                    <td className="num px-3 py-1.5 text-right text-[11px] text-muted">
                      {d.detected_at ? relTime(d.detected_at) : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </>
  );
}

function TallyList({
  tally,
  empty,
}: {
  tally: Record<string, number>;
  empty: string;
}) {
  const entries = Object.entries(tally).sort((a, b) => b[1] - a[1]);
  if (entries.length === 0)
    return <div className="px-4 py-6 text-center text-sm text-muted">{empty}</div>;
  return (
    <div className="space-y-1.5 p-4">
      {entries.map(([k, v]) => (
        <div key={k} className="flex items-center justify-between text-sm">
          <span className="text-paper/80">{titleCase(k.replace(/_/g, " "))}</span>
          <span className="num text-brass">{v.toLocaleString("en-IN")}</span>
        </div>
      ))}
    </div>
  );
}
