"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { useAsync, useLocalStorage } from "@/lib/hooks";
import type { JobRun, JobType } from "@/lib/types";
import { relTime, shortDate, DASH } from "@/lib/format";
import { Panel, PanelHeader } from "@/components/Panel";
import { JobStatusDot } from "@/components/JobStatus";
import { ErrorState, TableSkeleton, EmptyState } from "@/components/States";

const SAFE_JOBS: { job: JobType; label: string; desc: string }[] = [
  { job: "discover", label: "Discover", desc: "find new listings" },
  { job: "refresh", label: "Refresh", desc: "re-pull snapshots" },
  { job: "enrich", label: "Enrich", desc: "screener detail" },
  { job: "score", label: "Score", desc: "recompute composites" },
];

export default function JobsPage() {
  const [typeFilter, setTypeFilter] = useState("");
  const [adminToken, setAdminToken] = useLocalStorage<string>("wi.adminToken", "");
  const [batch, setBatch] = useState(5);
  const [launching, setLaunching] = useState<string | null>(null);
  const [toast, setToast] = useState<{ msg: string; ok: boolean } | null>(null);

  const fetcher = useCallback(
    (s: AbortSignal) =>
      api.adminJobs({ limit: 50, type: typeFilter || undefined }, s),
    [typeFilter],
  );
  const { data, loading, error, refetch } = useAsync<JobRun[]>(fetcher, [
    typeFilter,
  ]);

  const anyRunning = (data ?? []).some((j) => j.status === "running");

  // Auto-poll while a job is running.
  const refetchRef = useRef(refetch);
  refetchRef.current = refetch;
  useEffect(() => {
    if (!anyRunning) return;
    const id = setInterval(() => refetchRef.current(), 3500);
    return () => clearInterval(id);
  }, [anyRunning]);

  async function launch(job: JobType, args: Record<string, unknown> = {}) {
    setLaunching(job);
    setToast(null);
    try {
      await api.runJob(job, args, adminToken || undefined);
      setToast({ msg: `Launched ${job}. Polling for progress…`, ok: true });
      setTimeout(() => refetchRef.current(), 800);
    } catch (e) {
      const msg =
        e instanceof ApiError ? e.message : "Failed to launch job.";
      setToast({ msg, ok: false });
    } finally {
      setLaunching(null);
    }
  }

  return (
    <div className="space-y-5">
      <div className="flex items-baseline justify-between">
        <h1 className="serif text-2xl font-semibold text-paper">Jobs</h1>
        {anyRunning && (
          <span className="num flex items-center gap-1.5 text-xs text-brass">
            <span className="skeleton inline-block h-1.5 w-1.5 rounded-full" />
            live · polling
          </span>
        )}
      </div>

      {/* control panel */}
      <Panel>
        <PanelHeader
          title="Trigger a run"
          hint="launches python -m engine.run <job> as a subprocess"
        />
        <div className="space-y-4 p-4">
          <div className="flex flex-wrap gap-2">
            {SAFE_JOBS.map((s) => (
              <button
                key={s.job}
                onClick={() => launch(s.job)}
                disabled={launching !== null}
                className="group flex flex-col rounded-sm border border-hairline bg-panel2 px-3 py-2 text-left transition-colors hover:border-brass disabled:opacity-50"
              >
                <span className="text-sm font-medium text-paper group-hover:text-brass">
                  {launching === s.job ? "launching…" : s.label}
                </span>
                <span className="text-[10px] text-muted">{s.desc}</span>
              </button>
            ))}
          </div>

          {/* analyze — gated */}
          <div className="rounded-sm border border-terracotta/30 bg-terracotta/5 p-3">
            <div className="flex flex-wrap items-end justify-between gap-3">
              <div>
                <span className="text-xs font-semibold uppercase tracking-wide text-terracotta">
                  Analyze — spends Fable / Max limits
                </span>
                <p className="mt-0.5 text-[11px] text-muted">
                  Runs the council on a small explicit batch only. No bulk runs.
                </p>
              </div>
              <div className="flex items-end gap-2">
                <label className="block">
                  <span className="mb-1 block text-[10px] uppercase tracking-wide text-muted">
                    Batch size
                  </span>
                  <input
                    type="number"
                    min={1}
                    max={25}
                    value={batch}
                    onChange={(e) =>
                      setBatch(
                        Math.max(1, Math.min(25, Number(e.target.value) || 1)),
                      )
                    }
                    className="num w-20 rounded-sm border border-hairline bg-panel px-2 py-1.5 text-sm text-paper outline-none focus:border-brass"
                  />
                </label>
                <button
                  onClick={() => launch("analyze", { limit: batch })}
                  disabled={launching !== null}
                  className="rounded-sm border border-terracotta/50 bg-terracotta/10 px-3 py-2 text-sm font-medium text-terracotta transition-colors hover:bg-terracotta/20 disabled:opacity-50"
                >
                  {launching === "analyze" ? "launching…" : `Analyze ${batch}`}
                </button>
              </div>
            </div>
          </div>

          {/* admin token */}
          <div className="flex flex-wrap items-center gap-2 border-t border-hairline/60 pt-3">
            <label className="text-[10px] uppercase tracking-wide text-muted">
              X-Admin-Token
            </label>
            <input
              type="password"
              value={adminToken}
              onChange={(e) => setAdminToken(e.target.value)}
              placeholder="optional — localhost allowed without it"
              className="num flex-1 rounded-sm border border-hairline bg-panel px-2 py-1.5 text-xs text-paper outline-none focus:border-brass"
            />
          </div>

          {toast && (
            <div
              className={`rounded-sm border px-3 py-2 text-xs ${
                toast.ok
                  ? "border-sage/40 bg-sage/10 text-sage"
                  : "border-terracotta/40 bg-terracotta/10 text-terracotta"
              }`}
              role="status"
            >
              {toast.msg}
            </div>
          )}
        </div>
      </Panel>

      {/* jobs table */}
      <Panel>
        <PanelHeader
          title="Run history"
          right={
            <select
              value={typeFilter}
              onChange={(e) => setTypeFilter(e.target.value)}
              className="rounded-sm border border-hairline bg-panel px-2 py-1 text-xs text-paper outline-none focus:border-brass"
            >
              <option value="">All types</option>
              {["discover", "refresh", "enrich", "analyze", "score"].map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          }
        />
        {error ? (
          <ErrorState message={error} onRetry={refetch} />
        ) : loading && !data ? (
          <TableSkeleton rows={8} />
        ) : (data?.length ?? 0) === 0 ? (
          <EmptyState title="No job runs yet." hint="Trigger a run above to populate the history." />
        ) : (
          <div className="w-full overflow-x-auto">
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="border-b border-hairline text-[10px] uppercase tracking-wide text-muted">
                  <th className="px-3 py-2 text-left font-semibold">Type</th>
                  <th className="px-3 py-2 text-left font-semibold">Target</th>
                  <th className="px-3 py-2 text-left font-semibold">Status</th>
                  <th className="px-3 py-2 text-right font-semibold">Duration</th>
                  <th className="px-3 py-2 text-left font-semibold">Started</th>
                  <th className="px-3 py-2 text-left font-semibold">Stats</th>
                </tr>
              </thead>
              <tbody>
                {data!.map((j) => (
                  <tr
                    key={j.id}
                    className="border-b border-hairline/50 align-top last:border-0"
                  >
                    <td className="px-3 py-2">
                      <span className="num text-xs font-medium text-paper">
                        {j.job_type}
                      </span>
                    </td>
                    <td className="px-3 py-2 text-xs text-muted">
                      {j.target ?? DASH}
                    </td>
                    <td className="px-3 py-2">
                      <JobStatusDot status={j.status} />
                      {j.error && (
                        <div className="mt-1 max-w-[240px] truncate text-[10px] text-terracotta" title={j.error}>
                          {j.error}
                        </div>
                      )}
                    </td>
                    <td className="num px-3 py-2 text-right text-xs text-paper/90">
                      {j.duration_s == null ? DASH : `${j.duration_s.toFixed(1)}s`}
                    </td>
                    <td className="px-3 py-2 text-xs text-muted" title={shortDate(j.started_at)}>
                      {relTime(j.started_at)}
                    </td>
                    <td className="px-3 py-2">
                      {j.stats && Object.keys(j.stats).length > 0 ? (
                        <div className="num flex flex-wrap gap-x-2 gap-y-0.5 text-[10px] text-muted">
                          {Object.entries(j.stats)
                            .slice(0, 6)
                            .map(([k, v]) => (
                              <span key={k}>
                                {k}=
                                <span className="text-paper/90">{String(v)}</span>
                              </span>
                            ))}
                        </div>
                      ) : (
                        <span className="text-xs text-muted">{DASH}</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}
