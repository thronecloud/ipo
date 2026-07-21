"use client";

import { useLayoutEffect, useMemo, useRef, useState } from "react";
import type { MouseEvent as ReactMouseEvent } from "react";
import type { EquityPoint } from "@/lib/types";

// Inline-SVG equity curve: an equal-weighted BUY-pick portfolio (rebased to 100
// at entry) against the same picks' benchmark, indexed by trading days held.
// Crosshair hover reads out both values and the basket size at that day.

const H = 300;
const PAD = { top: 14, right: 14, bottom: 26, left: 46 };

function linePath(
  pts: EquityPoint[],
  pick: (p: EquityPoint) => number | null,
  x: (t: number) => number,
  y: (v: number) => number,
): string {
  let d = "";
  let pen = false; // whether the last point was drawn (breaks on nulls)
  for (const p of pts) {
    const v = pick(p);
    if (v == null) {
      pen = false;
      continue;
    }
    d += `${pen ? "L" : "M"}${x(p.t).toFixed(1)} ${y(v).toFixed(1)} `;
    pen = true;
  }
  return d.trim();
}

// Filled polygon for the p5/p95 uncertainty band: p95 forward along the top,
// p5 back along the bottom. Contiguous runs only — a gap (a point without a
// band) closes the current polygon so the fill never bridges missing data.
function bandArea(
  pts: EquityPoint[],
  x: (t: number) => number,
  y: (v: number) => number,
): string {
  let d = "";
  let run: EquityPoint[] = [];
  const flush = () => {
    if (run.length >= 2) {
      run.forEach((p, i) => {
        d += `${i ? "L" : "M"}${x(p.t).toFixed(1)} ${y(p.p95 as number).toFixed(1)} `;
      });
      for (let i = run.length - 1; i >= 0; i--) {
        const p = run[i];
        d += `L${x(p.t).toFixed(1)} ${y(p.p5 as number).toFixed(1)} `;
      }
      d += "Z ";
    }
    run = [];
  };
  for (const p of pts) {
    if (p.p5 != null && p.p95 != null) run.push(p);
    else flush();
  }
  flush();
  return d.trim();
}

export default function EquityCurve({
  points,
  benchmarkLabel,
  loading = false,
}: {
  points: EquityPoint[];
  benchmarkLabel: string;
  loading?: boolean;
}) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(720);
  const [hoverT, setHoverT] = useState<number | null>(null);

  useLayoutEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setW(el.clientWidth));
    ro.observe(el);
    setW(el.clientWidth);
    return () => ro.disconnect();
  }, []);

  const geom = useMemo(() => {
    const maxT = points.length ? points[points.length - 1].t : 1;
    const vals: number[] = [];
    for (const p of points) {
      vals.push(p.portfolio);
      if (p.net_portfolio != null) vals.push(p.net_portfolio);
      if (p.benchmark != null) vals.push(p.benchmark);
      if (p.p5 != null) vals.push(p.p5);
      if (p.p95 != null) vals.push(p.p95);
    }
    let lo = vals.length ? Math.min(...vals, 100) : 90;
    let hi = vals.length ? Math.max(...vals, 100) : 110;
    if (hi - lo < 1) {
      lo -= 1;
      hi += 1;
    }
    const padV = (hi - lo) * 0.08;
    lo -= padV;
    hi += padV;
    const plotW = Math.max(1, w - PAD.left - PAD.right);
    const plotH = H - PAD.top - PAD.bottom;
    const x = (t: number) => PAD.left + (maxT ? (t / maxT) * plotW : plotW / 2);
    const y = (v: number) => PAD.top + (1 - (v - lo) / (hi - lo)) * plotH;
    return { maxT, lo, hi, x, y, plotW };
  }, [points, w]);

  const byT = useMemo(() => {
    const m = new Map<number, EquityPoint>();
    for (const p of points) m.set(p.t, p);
    return m;
  }, [points]);

  const hovered = hoverT != null ? byT.get(hoverT) ?? null : null;

  // y-axis ticks: rebase baseline (100) plus domain ends.
  const yTicks = useMemo(() => {
    const set = new Set<number>([100]);
    set.add(Math.round(geom.lo + (geom.hi - geom.lo) * 0.5));
    set.add(Math.round(geom.hi - (geom.hi - geom.lo) * 0.06));
    set.add(Math.round(geom.lo + (geom.hi - geom.lo) * 0.06));
    return [...set].filter((v) => v > geom.lo && v < geom.hi).sort((a, b) => a - b);
  }, [geom]);

  function onMove(e: ReactMouseEvent<SVGSVGElement>) {
    if (!points.length) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const px = ((e.clientX - rect.left) / rect.width) * w;
    const frac = (px - PAD.left) / geom.plotW;
    const t = Math.round(Math.min(1, Math.max(0, frac)) * geom.maxT);
    // snap to the nearest offset that actually has a point
    let best: number | null = null;
    let bestD = Infinity;
    for (const p of points) {
      const d = Math.abs(p.t - t);
      if (d < bestD) {
        bestD = d;
        best = p.t;
      }
    }
    setHoverT(best);
  }

  if (!points.length) {
    return (
      <div className="py-16 text-center text-sm text-muted">
        No BUY picks in this selection yet — nothing to plot.
      </div>
    );
  }

  const portfolioPath = linePath(points, (p) => p.portfolio, geom.x, geom.y);
  const netPath = linePath(points, (p) => p.net_portfolio, geom.x, geom.y);
  const benchPath = linePath(points, (p) => p.benchmark, geom.x, geom.y);
  const bandPath = bandArea(points, geom.x, geom.y);
  const hasBand = bandPath.length > 0;
  const last = points[points.length - 1];
  const finalRet = last.portfolio - 100;
  const finalNetRet =
    last.net_portfolio != null ? last.net_portfolio - 100 : null;
  const finalExcess =
    last.benchmark != null ? last.portfolio - last.benchmark : null;

  return (
    <div ref={wrapRef} className="relative w-full">
      <div className="mb-2 flex flex-wrap items-center gap-4 text-xs">
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-[3px] w-4 rounded-full bg-brass" />
          <span className="text-paper/80">Selected council</span>
        </span>
        <span
          className="flex items-center gap-1.5"
          title="The same basket after trading costs (85 bps a side, charged on both the buy and the sell) — what you could actually keep."
        >
          <span
            className="inline-block h-0 w-4 border-t-2 border-dashed border-brass/70"
          />
          <span className="text-muted">After friction</span>
        </span>
        <span className="flex items-center gap-1.5">
          <span
            className="inline-block h-[3px] w-4 rounded-full"
            style={{ backgroundColor: "var(--color-muted)" }}
          />
          <span className="text-muted">{benchmarkLabel}</span>
        </span>
        {hasBand && (
          <span
            className="flex items-center gap-1.5"
            title="5th–95th percentile of the portfolio's value from resampling the picks — how much the path could wander given so few names."
          >
            <span className="inline-block h-2.5 w-4 rounded-[1px] bg-brass/20" />
            <span className="text-muted">5–95% band</span>
          </span>
        )}
        <span className="num ml-auto text-muted">
          {last.t}d held ·{" "}
          <span style={{ color: finalRet >= 0 ? "var(--color-sage)" : "var(--color-terracotta)" }}>
            {finalRet >= 0 ? "+" : ""}
            {finalRet.toFixed(1)}%
          </span>
          {finalNetRet != null && (
            <span title="After 85 bps/side trading costs on both legs.">
              {" "}
              <span className="text-muted">net</span>{" "}
              <span
                style={{ color: finalNetRet >= 0 ? "var(--color-sage)" : "var(--color-terracotta)" }}
              >
                {finalNetRet >= 0 ? "+" : ""}
                {finalNetRet.toFixed(1)}%
              </span>
            </span>
          )}
          {finalExcess != null && (
            <>
              {" "}
              <span className="text-muted">vs bench</span>{" "}
              <span
                style={{ color: finalExcess >= 0 ? "var(--color-sage)" : "var(--color-terracotta)" }}
              >
                {finalExcess >= 0 ? "+" : ""}
                {finalExcess.toFixed(1)}
              </span>
            </>
          )}
        </span>
      </div>

      <svg
        width={w}
        height={H}
        role="img"
        aria-label="Equity curve of the selected council's BUY picks versus the benchmark"
        onMouseMove={onMove}
        onMouseLeave={() => setHoverT(null)}
        className={loading ? "opacity-50 transition-opacity" : "transition-opacity"}
      >
        {/* y gridlines + labels */}
        {yTicks.map((v) => (
          <g key={v}>
            <line
              x1={PAD.left}
              x2={w - PAD.right}
              y1={geom.y(v)}
              y2={geom.y(v)}
              stroke="var(--color-hairline)"
              strokeWidth={1}
              strokeDasharray={v === 100 ? "3 3" : undefined}
              opacity={v === 100 ? 0.9 : 0.45}
            />
            <text
              x={PAD.left - 6}
              y={geom.y(v) + 3}
              textAnchor="end"
              className="num"
              fontSize={10}
              fill="var(--color-muted)"
            >
              {v}
            </text>
          </g>
        ))}

        {/* x-axis label */}
        <text
          x={(PAD.left + w - PAD.right) / 2}
          y={H - 6}
          textAnchor="middle"
          fontSize={10}
          fill="var(--color-muted)"
        >
          trading days held
        </text>

        {/* uncertainty band, drawn first so both lines sit on top of it */}
        {hasBand && (
          <path d={bandPath} fill="var(--color-brass)" fillOpacity={0.14} stroke="none" />
        )}
        <path d={benchPath} fill="none" stroke="var(--color-muted)" strokeWidth={1.5} />
        {/* net-of-friction line: same colour as gross, dashed + thinner so it
            reads as "the gross basket, minus costs" without a new hue. */}
        <path
          d={netPath}
          fill="none"
          stroke="var(--color-brass)"
          strokeWidth={1.5}
          strokeDasharray="4 3"
          opacity={0.75}
        />
        <path d={portfolioPath} fill="none" stroke="var(--color-brass)" strokeWidth={2} />

        {/* crosshair */}
        {hovered && (
          <g>
            <line
              x1={geom.x(hovered.t)}
              x2={geom.x(hovered.t)}
              y1={PAD.top}
              y2={H - PAD.bottom}
              stroke="var(--color-brass)"
              strokeWidth={1}
              strokeDasharray="3 3"
              opacity={0.7}
            />
            <circle cx={geom.x(hovered.t)} cy={geom.y(hovered.portfolio)} r={3.5} fill="var(--color-brass)" />
            {hovered.net_portfolio != null && (
              <circle
                cx={geom.x(hovered.t)}
                cy={geom.y(hovered.net_portfolio)}
                r={3}
                fill="none"
                stroke="var(--color-brass)"
                strokeWidth={1.5}
                opacity={0.75}
              />
            )}
            {hovered.benchmark != null && (
              <circle
                cx={geom.x(hovered.t)}
                cy={geom.y(hovered.benchmark)}
                r={3.5}
                fill="var(--color-muted)"
              />
            )}
          </g>
        )}
      </svg>

      {hovered && (
        <div
          className="pointer-events-none absolute top-8 rounded-sm border border-hairline bg-ink/95 px-2.5 py-1.5 text-[11px] shadow-lg"
          style={{
            left: Math.min(
              Math.max(geom.x(hovered.t) + 8, PAD.left),
              w - 150,
            ),
          }}
        >
          <div className="num mb-0.5 text-muted">Day {hovered.t}</div>
          <div className="num flex items-center gap-1.5">
            <span className="inline-block h-2 w-2 rounded-full bg-brass" />
            <span className="text-paper">{hovered.portfolio.toFixed(1)}</span>
            <span className="text-muted">council</span>
          </div>
          {hovered.net_portfolio != null && (
            <div className="num flex items-center gap-1.5">
              <span className="inline-block h-0 w-2 border-t-2 border-dashed border-brass/70" />
              <span className="text-paper">{hovered.net_portfolio.toFixed(1)}</span>
              <span className="text-muted">after friction</span>
            </div>
          )}
          {hovered.benchmark != null && (
            <div className="num flex items-center gap-1.5">
              <span
                className="inline-block h-2 w-2 rounded-full"
                style={{ backgroundColor: "var(--color-muted)" }}
              />
              <span className="text-paper">{hovered.benchmark.toFixed(1)}</span>
              <span className="text-muted">benchmark</span>
            </div>
          )}
          <div className="num mt-0.5 text-muted">{hovered.n} in basket</div>
        </div>
      )}
    </div>
  );
}
