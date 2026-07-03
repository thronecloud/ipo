"use client";

import Link from "next/link";
import { useCallback } from "react";
import { api } from "@/lib/api";
import { useAsync } from "@/lib/hooks";
import type { AdminOverview, PersonaDistribution, Usage } from "@/lib/types";
import { PERSONA_ORDER } from "@/lib/personas";
import { num, relTime, titleCase, DASH } from "@/lib/format";
import StatCard from "@/components/StatCard";
import { Panel, PanelHeader } from "@/components/Panel";
import { StackedBar, MiniBars } from "@/components/Bars";
import { JobStatusDot } from "@/components/JobStatus";
import { ErrorState, Skeleton } from "@/components/States";

export default function AdminOverviewPage() {
  const ovFetcher = useCallback((s: AbortSignal) => api.adminOverview(s), []);
  const { data, loading, error, refetch } = useAsync<AdminOverview>(
    ovFetcher,
    [],
  );
  const pFetcher = useCallback((s: AbortSignal) => api.adminPersonas(s), []);
  const { data: personas } = useAsync<PersonaDistribution[]>(pFetcher, []);
  const uFetcher = useCallback((s: AbortSignal) => api.adminUsage(s), []);
  const { data: usage } = useAsync<Usage>(uFetcher, []);

  if (error) {
    return (
      <div className="rounded-md border border-hairline bg-panel">
        <ErrorState message={error} onRetry={refetch} />
      </div>
    );
  }

  if (loading && !data) {
    return (
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
        {Array.from({ length: 6 }).map((_, i) => (
          <Skeleton key={i} style={{ height: 82 }} />
        ))}
      </div>
    );
  }

  if (!data) return null;

  const analyzedPct =
    data.stocks_total > 0
      ? Math.round((data.scored / data.stocks_total) * 100)
      : 0;

  const totalCost = usage
    ? Object.values(usage.by_model).reduce((a, m) => a + m.total_cost_usd, 0)
    : null;

  const personaOrdered = personas
    ? PERSONA_ORDER.map((p) => personas.find((d) => d.persona === p.slug)).filter(
        (x): x is PersonaDistribution => Boolean(x),
      )
    : [];

  return (
    <div className="space-y-5">
      <div className="flex items-baseline justify-between">
        <h1 className="serif text-2xl font-semibold text-paper">
          Engine Overview
        </h1>
        <span className="num text-xs text-muted">
          last job {relTime(data.recent_jobs[0]?.started_at ?? null)}
        </span>
      </div>

      {/* health cards */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
        <StatCard
          label="Stocks"
          value={num(data.stocks_total, { decimals: 0 })}
          sub={`${Object.entries(data.by_status)
            .map(([k, v]) => `${v} ${k}`)
            .join(" · ")}`}
        />
        <StatCard
          label="yFinance"
          value={num(data.snapshots.yfinance, { decimals: 0 })}
          sub="snapshots"
        />
        <StatCard
          label="Screener"
          value={num(data.snapshots.screener, { decimals: 0 })}
          sub="snapshots"
        />
        <StatCard
          label="Analyses"
          value={num(data.analyses_total, { decimals: 0 })}
          sub="persona verdicts"
        />
        <StatCard
          label="Scored"
          value={num(data.scored, { decimals: 0 })}
          sub={`${analyzedPct}% of universe`}
          accent="var(--color-sage)"
        />
        <StatCard
          label="Backlog"
          value={num(data.analysis_backlog, { decimals: 0 })}
          sub="awaiting the council"
          accent="var(--color-brass)"
        />
      </div>

      <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
        {/* coverage */}
        <Panel>
          <PanelHeader
            title="Coverage by universe"
            right={
              <Link
                href="/admin/coverage"
                className="text-xs text-brass hover:underline"
              >
                detail →
              </Link>
            }
          />
          <div className="space-y-3 p-4">
            {data.coverage.length === 0 && (
              <p className="text-sm text-muted">No coverage data.</p>
            )}
            {data.coverage.map((c) => (
              <div key={c.universe}>
                <div className="mb-1 flex items-baseline justify-between text-xs">
                  <span className="text-paper/90">{titleCase(c.universe)}</span>
                  <span className="num text-muted">
                    <span className="text-sage">{c.analyzed}</span> /{" "}
                    <span className="text-brass">{c.full_data}</span> /{" "}
                    <span className="text-paper">{c.tracked}</span>
                  </span>
                </div>
                <StackedBar
                  total={c.tracked}
                  segments={[
                    {
                      value: c.analyzed,
                      color: "var(--color-sage)",
                      label: "analyzed",
                    },
                    {
                      value: Math.max(0, c.full_data - c.analyzed),
                      color: "var(--color-brass)",
                      label: "full data",
                    },
                    {
                      value: Math.max(0, c.tracked - c.full_data),
                      color: "var(--color-hairline)",
                      label: "tracked",
                    },
                  ]}
                />
              </div>
            ))}
            <Legend />
          </div>
        </Panel>

        {/* freshness + jobs */}
        <div className="space-y-5">
          <Panel>
            <PanelHeader title="Snapshot freshness" />
            <div className="p-4">
              <StackedBar
                height={14}
                total={
                  data.freshness.fresh_24h +
                  data.freshness.stale_7d +
                  data.freshness.older
                }
                segments={[
                  {
                    value: data.freshness.fresh_24h,
                    color: "var(--color-sage)",
                    label: "fresh",
                  },
                  {
                    value: data.freshness.stale_7d,
                    color: "var(--color-brass)",
                    label: "stale",
                  },
                  {
                    value: data.freshness.older,
                    color: "var(--color-terracotta)",
                    label: "old",
                  },
                ]}
              />
              <div className="num mt-2 flex justify-between text-xs">
                <span className="text-sage">{data.freshness.fresh_24h} &lt;24h</span>
                <span className="text-brass">{data.freshness.stale_7d} &lt;7d</span>
                <span className="text-terracotta">{data.freshness.older} older</span>
              </div>
            </div>
          </Panel>

          <Panel>
            <PanelHeader
              title="Recent jobs"
              right={
                <Link
                  href="/admin/jobs"
                  className="text-xs text-brass hover:underline"
                >
                  all →
                </Link>
              }
            />
            <div className="divide-y divide-hairline/60">
              {data.recent_jobs.length === 0 && (
                <p className="px-4 py-6 text-sm text-muted">No jobs yet.</p>
              )}
              {data.recent_jobs.map((j) => (
                <div
                  key={j.id}
                  className="flex items-center justify-between gap-3 px-4 py-2.5"
                >
                  <div className="flex items-center gap-3">
                    <span className="num text-xs font-medium text-paper">
                      {j.job_type}
                    </span>
                    {j.target && (
                      <span className="text-[11px] text-muted">{j.target}</span>
                    )}
                  </div>
                  <div className="flex items-center gap-3">
                    <span className="num text-[11px] text-muted">
                      {relTime(j.started_at)}
                    </span>
                    <JobStatusDot status={j.status} />
                  </div>
                </div>
              ))}
            </div>
          </Panel>
        </div>
      </div>

      {/* persona distribution + usage */}
      <div className="grid grid-cols-1 gap-5 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <Panel>
          <PanelHeader
            title="Per-persona distribution"
            right={
              <Link
                href="/admin/personas"
                className="text-xs text-brass hover:underline"
              >
                detail →
              </Link>
            }
          />
          <div className="grid grid-cols-2 gap-x-6 gap-y-3 p-4 sm:grid-cols-3 lg:grid-cols-5">
            {personaOrdered.length === 0 &&
              Array.from({ length: 10 }).map((_, i) => (
                <Skeleton key={i} style={{ height: 48 }} />
              ))}
            {personaOrdered.map((p) => (
              <div key={p.persona}>
                <div className="mb-1 flex items-baseline justify-between">
                  <span className="truncate text-[11px] text-paper/90">
                    {PERSONA_ORDER.find((x) => x.slug === p.persona)?.short ??
                      p.display_name}
                  </span>
                  <span className="num text-[10px] text-muted">
                    {p.avg_score == null ? DASH : p.avg_score.toFixed(1)}
                  </span>
                </div>
                <MiniBars values={p.score_hist} height={24} />
                <div className="num mt-1 flex gap-2 text-[9px] text-muted">
                  <span className="text-sage">{p.rec_counts.BUY ?? 0}</span>
                  <span className="text-brass">{p.rec_counts.HOLD ?? 0}</span>
                  <span className="text-terracotta">{p.rec_counts.AVOID ?? 0}</span>
                </div>
              </div>
            ))}
          </div>
        </Panel>

        <Panel>
          <PanelHeader title="Usage & cost" />
          <div className="p-4">
            {!usage ? (
              <Skeleton style={{ height: 80 }} />
            ) : (
              <>
                <div className="mb-3 flex items-baseline justify-between">
                  <span className="text-xs text-muted">Total spend</span>
                  <span className="num text-xl font-semibold text-brass">
                    ${totalCost?.toFixed(2) ?? "0.00"}
                  </span>
                </div>
                <div className="space-y-2">
                  {Object.entries(usage.by_model).map(([model, m]) => (
                    <div
                      key={model}
                      className="flex items-center justify-between rounded-sm border border-hairline bg-panel2 px-2.5 py-1.5"
                    >
                      <div className="min-w-0">
                        <div className="num truncate text-[11px] text-paper">
                          {model}
                        </div>
                        <div className="num text-[10px] text-muted">
                          {m.analyses} runs ·{" "}
                          {((m.input_tokens + m.output_tokens) / 1000).toFixed(0)}k tok
                        </div>
                      </div>
                      <span className="num text-xs text-sage">
                        ${m.total_cost_usd.toFixed(2)}
                      </span>
                    </div>
                  ))}
                  {Object.keys(usage.by_model).length === 0 && (
                    <p className="text-sm text-muted">No usage recorded.</p>
                  )}
                </div>
              </>
            )}
          </div>
        </Panel>
      </div>
    </div>
  );
}

function Legend() {
  return (
    <div className="flex flex-wrap gap-3 pt-1 text-[10px] text-muted">
      {[
        ["analyzed", "var(--color-sage)"],
        ["full data", "var(--color-brass)"],
        ["tracked", "var(--color-hairline)"],
      ].map(([label, color]) => (
        <span key={label} className="inline-flex items-center gap-1.5">
          <span
            className="inline-block h-2 w-2 rounded-[1px]"
            style={{ background: color }}
          />
          {label}
        </span>
      ))}
    </div>
  );
}
