"use client";

import { useCallback } from "react";
import { api } from "@/lib/api";
import { useAsync } from "@/lib/hooks";
import type { CoverageRow } from "@/lib/types";
import { num, titleCase } from "@/lib/format";
import { Panel, PanelHeader } from "@/components/Panel";
import { StackedBar, Meter } from "@/components/Bars";
import { ErrorState, TableSkeleton, EmptyState } from "@/components/States";

export default function CoveragePage() {
  const fetcher = useCallback((s: AbortSignal) => api.adminCoverage(s), []);
  const { data, loading, error, refetch } = useAsync<CoverageRow[]>(fetcher, []);

  const totals = (data ?? []).reduce(
    (acc, c) => ({
      tracked: acc.tracked + c.tracked,
      full_data: acc.full_data + c.full_data,
      analyzed: acc.analyzed + c.analyzed,
    }),
    { tracked: 0, full_data: 0, analyzed: 0 },
  );

  return (
    <div className="space-y-5">
      <div className="flex items-baseline justify-between">
        <h1 className="serif text-2xl font-semibold text-paper">Coverage</h1>
        {data && (
          <span className="num text-xs text-muted">
            <span className="text-sage">{totals.analyzed}</span> analyzed /{" "}
            <span className="text-brass">{totals.full_data}</span> full /{" "}
            <span className="text-paper">{totals.tracked}</span> tracked
          </span>
        )}
      </div>

      <Panel>
        <PanelHeader
          title="Per-universe coverage"
          hint="analyzed → full-data → tracked"
        />
        {error ? (
          <ErrorState message={error} onRetry={refetch} />
        ) : loading && !data ? (
          <TableSkeleton rows={6} />
        ) : (data?.length ?? 0) === 0 ? (
          <EmptyState title="No universes tracked yet." />
        ) : (
          <div className="w-full overflow-x-auto">
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="border-b border-hairline text-[10px] uppercase tracking-wide text-muted">
                  <th className="px-3 py-2 text-left font-semibold">Universe</th>
                  <th className="px-3 py-2 text-right font-semibold">Tracked</th>
                  <th className="px-3 py-2 text-right font-semibold">Full data</th>
                  <th className="px-3 py-2 text-right font-semibold">Analyzed</th>
                  <th className="px-3 py-2 text-right font-semibold">Backlog</th>
                  <th className="w-[36%] px-3 py-2 text-left font-semibold">
                    Progress
                  </th>
                </tr>
              </thead>
              <tbody>
                {data!.map((c) => {
                  const backlog =
                    c.backlog ?? Math.max(0, c.full_data - c.analyzed);
                  const pctAnalyzed =
                    c.tracked > 0
                      ? Math.round((c.analyzed / c.tracked) * 100)
                      : 0;
                  return (
                    <tr
                      key={c.universe}
                      className="border-b border-hairline/50 last:border-0 hover:bg-panel2/50"
                    >
                      <td className="px-3 py-2.5 text-sm text-paper/90">
                        {titleCase(c.universe)}
                      </td>
                      <td className="num px-3 py-2.5 text-right text-sm text-paper">
                        {num(c.tracked, { decimals: 0 })}
                      </td>
                      <td className="num px-3 py-2.5 text-right text-sm text-brass">
                        {num(c.full_data, { decimals: 0 })}
                      </td>
                      <td className="num px-3 py-2.5 text-right text-sm text-sage">
                        {num(c.analyzed, { decimals: 0 })}
                      </td>
                      <td className="num px-3 py-2.5 text-right text-sm text-muted">
                        {num(backlog, { decimals: 0 })}
                      </td>
                      <td className="px-3 py-2.5">
                        <div className="flex items-center gap-2">
                          <div className="flex-1">
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
                                  label: "full",
                                },
                                {
                                  value: Math.max(0, c.tracked - c.full_data),
                                  color: "var(--color-hairline)",
                                  label: "tracked",
                                },
                              ]}
                            />
                          </div>
                          <span className="num w-9 text-right text-[11px] text-muted">
                            {pctAnalyzed}%
                          </span>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
              <tfoot>
                <tr className="border-t border-hairline">
                  <td className="px-3 py-2.5 text-xs font-semibold uppercase tracking-wide text-muted">
                    Total
                  </td>
                  <td className="num px-3 py-2.5 text-right text-sm text-paper">
                    {num(totals.tracked, { decimals: 0 })}
                  </td>
                  <td className="num px-3 py-2.5 text-right text-sm text-brass">
                    {num(totals.full_data, { decimals: 0 })}
                  </td>
                  <td className="num px-3 py-2.5 text-right text-sm text-sage">
                    {num(totals.analyzed, { decimals: 0 })}
                  </td>
                  <td className="num px-3 py-2.5 text-right text-sm text-muted">
                    {num(
                      Math.max(0, totals.full_data - totals.analyzed),
                      { decimals: 0 },
                    )}
                  </td>
                  <td className="px-3 py-2.5">
                    <Meter value={totals.analyzed} max={totals.tracked} />
                  </td>
                </tr>
              </tfoot>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}
