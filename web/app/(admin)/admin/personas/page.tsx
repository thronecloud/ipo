"use client";

import { useCallback } from "react";
import { api } from "@/lib/api";
import { useAsync } from "@/lib/hooks";
import type { PersonaDistribution } from "@/lib/types";
import { PERSONA_ORDER } from "@/lib/personas";
import { DASH } from "@/lib/format";
import { Panel } from "@/components/Panel";
import { MiniBars, StackedBar } from "@/components/Bars";
import { ErrorState, Skeleton, EmptyState } from "@/components/States";

export default function PersonasPage() {
  const fetcher = useCallback((s: AbortSignal) => api.adminPersonas(s), []);
  const { data, loading, error, refetch } = useAsync<PersonaDistribution[]>(
    fetcher,
    [],
  );

  const ordered = data
    ? PERSONA_ORDER.map((p) => data.find((d) => d.persona === p.slug)).filter(
        (x): x is PersonaDistribution => Boolean(x),
      )
    : [];

  return (
    <div className="space-y-5">
      <div className="flex items-baseline justify-between">
        <h1 className="serif text-2xl font-semibold text-paper">
          Persona distributions
        </h1>
        <span className="text-xs text-muted">
          score histograms &amp; call splits · drift detection
        </span>
      </div>

      {error ? (
        <div className="rounded-md border border-hairline bg-panel">
          <ErrorState message={error} onRetry={refetch} />
        </div>
      ) : loading && !data ? (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          {Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} style={{ height: 160 }} />
          ))}
        </div>
      ) : ordered.length === 0 ? (
        <div className="rounded-md border border-hairline bg-panel">
          <EmptyState title="No analyses yet." hint="The council has not scored any stocks." />
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          {ordered.map((p) => {
            const total =
              (p.rec_counts.BUY ?? 0) +
              (p.rec_counts.HOLD ?? 0) +
              (p.rec_counts.AVOID ?? 0);
            const persona = PERSONA_ORDER.find((x) => x.slug === p.persona);
            return (
              <Panel key={p.persona}>
                <div className="flex items-center justify-between border-b border-hairline px-4 py-2.5">
                  <div>
                    <h3 className="serif text-base font-medium text-paper">
                      {p.display_name}
                    </h3>
                    <span className="text-[10px] uppercase tracking-[0.14em] text-muted">
                      {persona?.nationality ?? ""}
                    </span>
                  </div>
                  <div className="text-right">
                    <div className="num text-lg font-semibold text-brass">
                      {p.avg_score == null ? DASH : p.avg_score.toFixed(2)}
                    </div>
                    <div className="num text-[10px] text-muted">
                      avg · {p.count} calls
                    </div>
                  </div>
                </div>
                <div className="p-4">
                  <span className="mb-1.5 block text-[10px] font-semibold uppercase tracking-wide text-muted">
                    Score histogram (0–10)
                  </span>
                  <MiniBars values={p.score_hist} height={44} color="var(--color-brass)" />
                  <div className="num mt-1 flex justify-between text-[9px] text-muted">
                    {Array.from({ length: 11 }).map((_, i) => (
                      <span key={i}>{i}</span>
                    ))}
                  </div>

                  <div className="mt-4">
                    <div className="mb-1 flex items-center justify-between">
                      <span className="text-[10px] font-semibold uppercase tracking-wide text-muted">
                        Call split
                      </span>
                      <span className="num text-[10px] text-muted">
                        <span className="text-sage">{p.rec_counts.BUY ?? 0}</span> ·{" "}
                        <span className="text-brass">{p.rec_counts.HOLD ?? 0}</span> ·{" "}
                        <span className="text-terracotta">{p.rec_counts.AVOID ?? 0}</span>
                      </span>
                    </div>
                    <StackedBar
                      height={10}
                      total={total}
                      segments={[
                        {
                          value: p.rec_counts.BUY ?? 0,
                          color: "var(--color-sage)",
                          label: "BUY",
                        },
                        {
                          value: p.rec_counts.HOLD ?? 0,
                          color: "var(--color-brass)",
                          label: "HOLD",
                        },
                        {
                          value: p.rec_counts.AVOID ?? 0,
                          color: "var(--color-terracotta)",
                          label: "AVOID",
                        },
                      ]}
                    />
                  </div>
                </div>
              </Panel>
            );
          })}
        </div>
      )}
    </div>
  );
}
