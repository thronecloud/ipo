"use client";

import { useCallback, useState } from "react";
import { api } from "@/lib/api";
import { useAsync } from "@/lib/hooks";
import type { JobRun, SchedulerJob, SchedulerOverview } from "@/lib/types";
import { relTime, shortDate, DASH } from "@/lib/format";
import { Panel, PanelHeader } from "@/components/Panel";
import { ErrorState, TableSkeleton, EmptyState } from "@/components/States";

const STATUS_COLOR: Record<string, string> = {
  running: "var(--color-brass)",
  success: "var(--color-sage)",
  partial: "var(--color-brass)",
  error: "var(--color-terracotta)",
};

function StatusChip({ status, error }: { status: string; error?: string | null }) {
  const color = STATUS_COLOR[status] ?? "var(--color-muted)";
  return (
    <span
      className="inline-flex items-center gap-1.5"
      title={error || undefined}
    >
      <span
        className="inline-block h-2 w-2 rounded-full"
        style={{ background: color }}
        aria-hidden
      />
      <span className="text-[11px] capitalize" style={{ color }}>
        {status}
      </span>
      {status === "running" && (
        <span className="skeleton inline-block h-1 w-6 rounded-full" aria-hidden />
      )}
    </span>
  );
}

function StatsInline({ stats }: { stats: JobRun["stats"] }) {
  if (!stats || Object.keys(stats).length === 0)
    return <span className="text-xs text-muted">{DASH}</span>;
  return (
    <div className="num flex flex-wrap gap-x-2 gap-y-0.5 text-[10px] text-muted">
      {Object.entries(stats)
        .slice(0, 6)
        .map(([k, v]) => (
          <span key={k}>
            {k}=<span className="text-paper/90">{String(v)}</span>
          </span>
        ))}
    </div>
  );
}

function DrillDown({ jobId }: { jobId: string }) {
  const fetcher = useCallback(
    (s: AbortSignal) => api.adminScheduler(jobId, s),
    [jobId],
  );
  const { data, loading, error, refetch } = useAsync<SchedulerOverview>(fetcher, [
    jobId,
  ]);
  const runs =
    data?.jobs.find((j) => j.id === jobId)?.recent_runs ?? [];

  if (error) return <ErrorState message={error} onRetry={refetch} />;
  if (loading && !data)
    return <div className="p-3"><TableSkeleton rows={4} /></div>;
  if (runs.length === 0)
    return (
      <div className="px-4 py-6">
        <EmptyState title="No runs recorded yet." />
      </div>
    );

  return (
    <div className="w-full overflow-x-auto border-t border-hairline bg-panel2/40">
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr className="border-b border-hairline text-[10px] uppercase tracking-wide text-muted">
            <th className="px-3 py-2 text-left font-semibold">Status</th>
            <th className="px-3 py-2 text-left font-semibold">Target</th>
            <th className="px-3 py-2 text-right font-semibold">Duration</th>
            <th className="px-3 py-2 text-left font-semibold">Started</th>
            <th className="px-3 py-2 text-left font-semibold">Stats</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((j) => (
            <tr
              key={j.id}
              className="border-b border-hairline/50 align-top last:border-0"
            >
              <td className="px-3 py-2">
                <StatusChip status={j.status} error={j.error} />
              </td>
              <td className="px-3 py-2 text-xs text-muted">{j.target ?? DASH}</td>
              <td className="num px-3 py-2 text-right text-xs text-paper/90">
                {j.duration_s == null ? DASH : `${j.duration_s.toFixed(1)}s`}
              </td>
              <td
                className="px-3 py-2 text-xs text-muted"
                title={shortDate(j.started_at)}
              >
                {relTime(j.started_at)}
              </td>
              <td className="px-3 py-2">
                <StatsInline stats={j.stats} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function JobRows({ job }: { job: SchedulerJob }) {
  const [open, setOpen] = useState(false);
  const last = job.last_run;
  const untracked = job.job_type === null;

  return (
    <>
      <tr
        className="cursor-pointer border-b border-hairline/50 align-top last:border-0 hover:bg-panel2/40"
        onClick={() => setOpen((v) => !v)}
      >
        <td className="px-3 py-2.5">
          <div className="flex items-center gap-1.5">
            <span
              className="text-muted transition-transform"
              style={{ transform: open ? "rotate(90deg)" : "none" }}
              aria-hidden
            >
              ▸
            </span>
            <span className="num text-xs font-medium text-paper">{job.id}</span>
          </div>
          <div className="mt-0.5 pl-4 text-[10px] leading-snug text-muted">
            {job.description}
          </div>
        </td>
        <td className="num px-3 py-2.5 text-xs text-paper/90">{job.cadence}</td>
        <td className="px-3 py-2.5">
          {untracked ? (
            <span className="text-[11px] text-muted" title="This job writes no job_run row, so its last run cannot be tracked.">
              untracked
            </span>
          ) : last ? (
            <div className="space-y-1">
              <StatusChip status={last.status} error={last.error} />
              <div
                className="text-[10px] text-muted"
                title={shortDate(last.started_at)}
              >
                {relTime(last.started_at)}
              </div>
              {last.error && (
                <div
                  className="max-w-[220px] truncate text-[10px] text-terracotta"
                  title={last.error}
                >
                  {last.error}
                </div>
              )}
            </div>
          ) : (
            <span className="text-[11px] text-muted">never run</span>
          )}
        </td>
        <td
          className="px-3 py-2.5 text-xs text-muted"
          title={shortDate(job.next_expected)}
        >
          {job.next_expected ? relTime(job.next_expected) : DASH}
        </td>
        <td className="px-3 py-2.5">
          {job.missed === true && (
            <span className="rounded-sm border border-terracotta/50 bg-terracotta/10 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-terracotta">
              Missed
            </span>
          )}
        </td>
      </tr>
      {open && (
        <tr>
          <td colSpan={5} className="p-0">
            <DrillDown jobId={job.id} />
          </td>
        </tr>
      )}
    </>
  );
}

export default function SchedulerPage() {
  const fetcher = useCallback((s: AbortSignal) => api.adminScheduler(undefined, s), []);
  const { data, loading, error, refetch } = useAsync<SchedulerOverview>(
    fetcher,
    [],
    { refreshMs: 15000 },
  );

  const missedCount = (data?.jobs ?? []).filter((j) => j.missed === true).length;

  return (
    <div className="space-y-5">
      <div className="flex items-baseline justify-between">
        <h1 className="serif text-2xl font-semibold text-paper">Scheduler</h1>
        {missedCount > 0 && (
          <span className="num text-xs font-semibold text-terracotta">
            {missedCount} missed
          </span>
        )}
      </div>

      <Panel>
        <PanelHeader
          title="Declared schedule vs. actual runs"
          hint="click a job for its recent runs"
        />
        {error ? (
          <ErrorState message={error} onRetry={refetch} />
        ) : loading && !data ? (
          <TableSkeleton rows={13} />
        ) : (data?.jobs.length ?? 0) === 0 ? (
          <EmptyState title="No scheduled jobs." />
        ) : (
          <div className="w-full overflow-x-auto">
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="border-b border-hairline text-[10px] uppercase tracking-wide text-muted">
                  <th className="px-3 py-2 text-left font-semibold">Job</th>
                  <th className="px-3 py-2 text-left font-semibold">Cadence</th>
                  <th className="px-3 py-2 text-left font-semibold">Last run</th>
                  <th className="px-3 py-2 text-left font-semibold">Next expected</th>
                  <th className="px-3 py-2 text-left font-semibold">Flag</th>
                </tr>
              </thead>
              <tbody>
                {data!.jobs.map((j) => (
                  <JobRows key={j.id} job={j} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}
