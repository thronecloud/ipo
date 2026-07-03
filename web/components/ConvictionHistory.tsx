"use client";

import type { ConvictionPoint } from "@/lib/types";
import { Sparkline } from "./Bars";
import { composite, shortDate } from "@/lib/format";

export default function ConvictionHistory({
  history,
}: {
  history: ConvictionPoint[];
}) {
  const points = history
    .filter((h) => h.composite_score !== null)
    .map((h, i) => ({
      x: i,
      y: h.composite_score,
      label: shortDate(h.computed_at),
    }));

  if (points.length === 0) {
    return (
      <p className="px-4 py-8 text-center text-sm text-muted">
        No conviction history recorded yet.
      </p>
    );
  }

  const first = points[0];
  const last = points.at(-1)!;
  const delta =
    points.length > 1 && first.y !== null && last.y !== null
      ? (last.y as number) - (first.y as number)
      : null;

  return (
    <div className="p-4">
      <div className="mb-2 flex items-baseline justify-between">
        <span className="num text-2xl font-semibold text-paper">
          {composite(last.y)}
          <span className="ml-1 text-xs text-muted">/100</span>
        </span>
        {delta !== null && (
          <span
            className="num text-sm"
            style={{
              color: delta >= 0 ? "var(--color-sage)" : "var(--color-terracotta)",
            }}
          >
            {delta >= 0 ? "+" : ""}
            {delta.toFixed(1)} since {first.label}
          </span>
        )}
      </div>
      <Sparkline points={points} />
      {points.length === 1 && (
        <p className="num mt-2 text-center text-[11px] text-muted">
          Single reading · {last.label} — history accrues as the engine re-scores.
        </p>
      )}
    </div>
  );
}
