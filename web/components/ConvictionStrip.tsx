"use client";

// ── The signature element ─────────────────────────────────────────
// Ten cells, one per council member, in the fixed PERSONA_ORDER. Each cell is
// colored by that persona's recommendation for this stock:
//   BUY = sage · HOLD = brass · AVOID = terracotta · none/unanalyzed = hairline
// Hover a cell for the persona's name + score. Used wherever a stock appears.

import { useState } from "react";
import {
  PERSONA_ORDER,
  REC_COLOR,
} from "@/lib/personas";
import type { PerPersonaVerdict, Recommendation } from "@/lib/types";
import { score10 } from "@/lib/format";

type Size = "xs" | "sm" | "lg";

const DIMS: Record<Size, { cell: number; gap: number; radius: number }> = {
  xs: { cell: 9, gap: 2, radius: 1 },
  sm: { cell: 14, gap: 3, radius: 2 },
  lg: { cell: 28, gap: 4, radius: 3 },
};

function cellColor(rec: Recommendation | null | undefined): string {
  if (rec && rec in REC_COLOR) return REC_COLOR[rec];
  return "var(--color-hairline)";
}

export interface ConvictionStripProps {
  perPersona: Record<string, PerPersonaVerdict>;
  size?: Size;
  className?: string;
  /** Show persona initials under each cell (lg only). */
  labeled?: boolean;
}

export default function ConvictionStrip({
  perPersona,
  size = "xs",
  className = "",
  labeled = false,
}: ConvictionStripProps) {
  const { cell, gap, radius } = DIMS[size];
  const [hover, setHover] = useState<number | null>(null);
  const anyAnalyzed = PERSONA_ORDER.some((p) => {
    const v = perPersona[p.slug];
    return v && v.recommendation;
  });

  return (
    <div
      className={`relative inline-flex flex-col ${className}`}
      role="img"
      aria-label={
        anyAnalyzed
          ? "Conviction strip: council recommendations"
          : "Not yet analyzed by the council"
      }
    >
      <div className="inline-flex items-end" style={{ gap }}>
        {PERSONA_ORDER.map((p, i) => {
          const v = perPersona[p.slug];
          const rec = v?.recommendation ?? null;
          const color = cellColor(rec);
          const unrated = !rec;
          return (
            <div
              key={p.slug}
              className="relative"
              onMouseEnter={() => setHover(i)}
              onMouseLeave={() => setHover((h) => (h === i ? null : h))}
            >
              <div
                style={{
                  width: cell,
                  height: cell,
                  background: color,
                  borderRadius: radius,
                  opacity: unrated ? 0.55 : 1,
                  border: unrated
                    ? "1px dashed rgba(138,147,160,0.35)"
                    : "none",
                  boxSizing: "border-box",
                }}
              />
              {labeled && size === "lg" && (
                <span className="mt-1 block text-center text-[9px] uppercase tracking-wide text-muted">
                  {p.short.slice(0, 3)}
                </span>
              )}
              {hover === i && (
                <div
                  className="pointer-events-none absolute bottom-full left-1/2 z-30 mb-1.5 -translate-x-1/2 whitespace-nowrap rounded border border-hairline bg-panel2 px-2 py-1 text-[11px] shadow-lg"
                  role="tooltip"
                >
                  <span className="font-medium text-paper">{p.name}</span>
                  <span className="mx-1.5 text-hairline">·</span>
                  {rec ? (
                    <span
                      className="num"
                      style={{ color: cellColor(rec) }}
                    >
                      {rec} {score10(v?.score)}
                    </span>
                  ) : (
                    <span className="text-muted">awaiting</span>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
