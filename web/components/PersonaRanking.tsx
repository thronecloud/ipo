"use client";

import { useMemo } from "react";
import type { CI, PersonaResult } from "@/lib/types";
import { PERSONA_BY_SLUG } from "@/lib/personas";
import { DASH } from "@/lib/format";

// Ranked comparison of every council member on one summary metric, at one
// horizon. Diverging bars around zero for excess/spread; a 0-100 fill for hit
// rate. Hovering any row shows the exact value; clicking toggles that investor
// in the selected subset.

export type Metric = "mean_excess" | "hit_rate" | "spread";

const METRIC_LABEL: Record<Metric, string> = {
  mean_excess: "Mean excess return vs benchmark",
  hit_rate: "Hit rate (share of picks that beat the benchmark)",
  spread: "BUY-minus-AVOID excess (does the call have an edge?)",
};

function valueOf(p: PersonaResult, metric: Metric, h: string): number | null {
  if (metric === "hit_rate") return p.stats.hit_rate?.[h] ?? null;
  if (metric === "spread") return p.spread?.[h] ?? null;
  return p.stats.mean_excess?.[h] ?? null;
}

function ciOf(p: PersonaResult, metric: Metric, h: string): CI | null {
  if (metric === "hit_rate") return p.stats.hit_rate_ci?.[h] ?? null;
  if (metric === "spread") return p.spread_ci?.[h] ?? null;
  return p.stats.mean_excess_ci?.[h] ?? null;
}

function fmt(v: number | null, metric: Metric): string {
  if (v == null) return DASH;
  if (metric === "hit_rate") return `${v.toFixed(0)}%`;
  const sign = v > 0 ? "+" : "";
  return `${sign}${v.toFixed(1)}`;
}

// e.g. "95% CI −1.2 to +4.8 · indistinguishable from zero"
function ciNote(ci: CI | null): string {
  if (!ci) return "too few names to bootstrap a CI";
  const f = (x: number) => `${x > 0 ? "+" : ""}${x.toFixed(1)}`;
  return `95% CI ${f(ci.ci_low)} to ${f(ci.ci_high)} · ${ci.verdict}`;
}

export default function PersonaRanking({
  personas,
  metric,
  horizon,
  selected,
  onToggle,
}: {
  personas: PersonaResult[];
  metric: Metric;
  horizon: number;
  selected: string[];
  onToggle: (slug: string) => void;
}) {
  const h = String(horizon);
  const sel = new Set(selected);

  const rows = useMemo(() => {
    const withVal = personas.map((p) => ({
      p,
      v: valueOf(p, metric, h),
      ci: ciOf(p, metric, h),
    }));
    // Nulls (no picks / no data at this horizon) sink to the bottom.
    return withVal.sort((a, b) => {
      if (a.v == null && b.v == null) return 0;
      if (a.v == null) return 1;
      if (b.v == null) return -1;
      return b.v - a.v;
    });
  }, [personas, metric, h]);

  const diverging = metric !== "hit_rate";
  const maxAbs = useMemo(() => {
    const vs = rows.map((r) => r.v).filter((v): v is number => v != null);
    if (!vs.length) return 1;
    if (diverging) return Math.max(1, ...vs.map((v) => Math.abs(v)));
    return 100;
  }, [rows, diverging]);

  return (
    <div>
      <p className="mb-3 text-xs text-muted" title={METRIC_LABEL[metric]}>
        {METRIC_LABEL[metric]}, at {horizon} trading days. Bars rank the whole
        council; click a name to add or drop it from the selected set above.
      </p>
      <div className="space-y-1">
        {rows.map(({ p, v, ci }) => {
          const meta = PERSONA_BY_SLUG[p.persona];
          const on = sel.has(p.persona);
          const pos = v != null && v >= 0;
          const frac = v == null ? 0 : Math.min(1, Math.abs(v) / maxAbs);
          // A value whose CI straddles its null is noise; mute it so the eye
          // doesn't rank on a number the data can't support.
          const noise = ci != null && ci.verdict === "indistinguishable from zero";
          const title =
            `${meta?.name ?? p.persona}: ${fmt(v, metric)} at ${horizon}d · ` +
            `${p.n_buy} BUY / ${p.n_avoid} AVOID picks · ${ciNote(ci)}`;
          return (
            <button
              key={p.persona}
              onClick={() => onToggle(p.persona)}
              aria-pressed={on}
              title={title}
              className={`flex w-full items-center gap-2 rounded-sm px-1.5 py-1 text-left transition-colors ${
                on ? "bg-brass/10" : "hover:bg-panel2"
              }`}
            >
              <span
                className={`w-24 shrink-0 truncate text-[11px] ${
                  on ? "text-paper" : "text-muted"
                }`}
              >
                {meta?.short ?? p.persona}
              </span>

              {diverging ? (
                // Diverging: negatives grow left of centre, positives right.
                <span className="relative flex h-3.5 flex-1 items-center">
                  <span className="absolute left-1/2 h-full w-px bg-hairline" />
                  {v != null && (
                    <span
                      className="absolute h-2 rounded-[1px]"
                      style={{
                        width: `${(frac * 50).toFixed(1)}%`,
                        [pos ? "left" : "right"]: "50%",
                        backgroundColor: pos
                          ? "var(--color-sage)"
                          : "var(--color-terracotta)",
                      }}
                    />
                  )}
                </span>
              ) : (
                <span className="relative h-3.5 flex-1 overflow-hidden rounded-[1px] bg-panel2">
                  {v != null && (
                    <span
                      className="absolute left-0 top-0 h-full rounded-[1px]"
                      style={{
                        width: `${(frac * 100).toFixed(1)}%`,
                        backgroundColor:
                          v >= 50 ? "var(--color-sage)" : "var(--color-brass)",
                      }}
                    />
                  )}
                </span>
              )}

              <span
                className="num w-14 shrink-0 text-right text-[11px]"
                style={{
                  color:
                    v == null || noise
                      ? "var(--color-muted)"
                      : diverging
                        ? pos
                          ? "var(--color-sage)"
                          : "var(--color-terracotta)"
                        : "var(--color-paper)",
                }}
              >
                {fmt(v, metric)}
              </span>
              <span className="num w-10 shrink-0 text-right text-[10px] text-muted">
                {p.n_buy}▲
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
