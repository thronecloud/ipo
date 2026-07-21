"use client";

import { useLayoutEffect, useMemo, useRef, useState } from "react";
import type { MouseEvent as ReactMouseEvent } from "react";
import type { CI, EraSlice, VintageStudy, VintageWindow } from "@/lib/types";
import { DASH, pct, shortDate } from "@/lib/format";
import { Panel, PanelHeader } from "@/components/Panel";

// "Signal over time": the event study rolled forward. Two inline-SVG charts,
// siblings to EquityCurve — (1) per-window mean excess vs benchmark with a
// bootstrap CI band and optional prompt-era / model-era split lines, and (2) an
// IC-decay curve showing how fast a verdict's information fades. Both are
// crosshair-hover-enabled and labelled in plain language.

const H = 280;
const PAD = { top: 16, right: 16, bottom: 30, left: 46 };

// Distinct, colour-blind-tolerant series colours for the era split. First entry
// matches the overall brass line so a single-era cohort reads consistently.
const ERA_COLORS = [
  "#c8a24b", // brass
  "#4fb286", // sage
  "#6ea8fe", // blue
  "#d9614c", // terracotta
  "#b98cff", // violet
  "#e6a3c9", // pink
];

const HORIZON_LABEL: Record<number, string> = {
  5: "1 week",
  21: "1 month",
  63: "3 months",
  126: "6 months",
};
function horizonLabel(h: number): string {
  return HORIZON_LABEL[h] ?? `${h}d`;
}

function ciRange(ci: CI | null | undefined): string {
  if (!ci) return "too few names to bootstrap a CI";
  const f = (x: number) => `${x > 0 ? "+" : ""}${x.toFixed(1)}`;
  return `95% CI ${f(ci.ci_low)} to ${f(ci.ci_high)} · ${ci.verdict}`;
}

function signColor(v: number | null | undefined): string {
  if (v == null) return "var(--color-muted)";
  return v >= 0 ? "var(--color-sage)" : "var(--color-terracotta)";
}

type EraAxis = "none" | "prompt_version" | "model";

// ── mean-excess-over-time chart ───────────────────────────────────

function useWidth(): [React.RefObject<HTMLDivElement | null>, number] {
  const ref = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(720);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setW(el.clientWidth));
    ro.observe(el);
    setW(el.clientWidth);
    return () => ro.disconnect();
  }, []);
  return [ref, w];
}

interface EraSeries {
  key: string;
  color: string;
  // value per window index (null = missing / insufficient), plus the CI for hover
  values: (number | null)[];
  cis: (CI | null)[];
}

function eraSeriesFor(windows: VintageWindow[], axis: EraAxis): EraSeries[] {
  if (axis === "none") return [];
  const keys = new Set<string>();
  for (const w of windows) for (const k of Object.keys(w.eras[axis])) keys.add(k);
  const sorted = [...keys].sort();
  return sorted.map((key, i) => ({
    key,
    color: ERA_COLORS[i % ERA_COLORS.length],
    values: windows.map((w) => {
      const slice: EraSlice | undefined = w.eras[axis][key];
      return slice && !slice.insufficient ? slice.mean_excess ?? null : null;
    }),
    cis: windows.map((w) => {
      const slice: EraSlice | undefined = w.eras[axis][key];
      return slice && !slice.insufficient ? slice.mean_excess_ci ?? null : null;
    }),
  }));
}

function ExcessOverTime({
  windows,
  axis,
  benchmark,
}: {
  windows: VintageWindow[];
  axis: EraAxis;
  benchmark: string;
}) {
  const [ref, w] = useWidth();
  const [hover, setHover] = useState<number | null>(null);

  const eraSeries = useMemo(() => eraSeriesFor(windows, axis), [windows, axis]);
  const showBand = axis === "none";

  const geom = useMemo(() => {
    const n = windows.length;
    const vals: number[] = [0];
    for (const win of windows) {
      if (win.mean_excess != null) vals.push(win.mean_excess);
      if (showBand && win.mean_excess_ci) {
        vals.push(win.mean_excess_ci.ci_low, win.mean_excess_ci.ci_high);
      }
    }
    for (const s of eraSeries)
      for (const v of s.values) if (v != null) vals.push(v);
    let lo = Math.min(...vals);
    let hi = Math.max(...vals);
    if (hi - lo < 1) {
      lo -= 1;
      hi += 1;
    }
    const padV = (hi - lo) * 0.1;
    lo -= padV;
    hi += padV;
    const plotW = Math.max(1, w - PAD.left - PAD.right);
    const plotH = H - PAD.top - PAD.bottom;
    const x = (i: number) =>
      PAD.left + (n > 1 ? (i / (n - 1)) * plotW : plotW / 2);
    const y = (v: number) => PAD.top + (1 - (v - lo) / (hi - lo)) * plotH;
    return { n, lo, hi, x, y, plotW, plotH };
  }, [windows, eraSeries, showBand, w]);

  function line(values: (number | null)[]): string {
    let d = "";
    let pen = false;
    values.forEach((v, i) => {
      if (v == null) {
        pen = false;
        return;
      }
      d += `${pen ? "L" : "M"}${geom.x(i).toFixed(1)} ${geom.y(v).toFixed(1)} `;
      pen = true;
    });
    return d.trim();
  }

  // CI band polygon (contiguous runs only, so gaps never bridge).
  const bandPath = useMemo(() => {
    if (!showBand) return "";
    let d = "";
    let run: { i: number; lo: number; hi: number }[] = [];
    const flush = () => {
      if (run.length >= 2) {
        run.forEach((p, k) => {
          d += `${k ? "L" : "M"}${geom.x(p.i).toFixed(1)} ${geom.y(p.hi).toFixed(1)} `;
        });
        for (let k = run.length - 1; k >= 0; k--)
          d += `L${geom.x(run[k].i).toFixed(1)} ${geom.y(run[k].lo).toFixed(1)} `;
        d += "Z ";
      }
      run = [];
    };
    windows.forEach((win, i) => {
      if (win.mean_excess_ci)
        run.push({ i, lo: win.mean_excess_ci.ci_low, hi: win.mean_excess_ci.ci_high });
      else flush();
    });
    flush();
    return d.trim();
  }, [windows, geom, showBand]);

  const overallPath = useMemo(
    () => line(windows.map((win) => win.mean_excess)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [windows, geom],
  );

  // date ticks: first / middle / last
  const xTicks = useMemo(() => {
    const n = windows.length;
    if (n === 0) return [] as number[];
    if (n === 1) return [0];
    return [0, Math.floor((n - 1) / 2), n - 1];
  }, [windows]);

  function onMove(e: ReactMouseEvent<SVGSVGElement>) {
    if (!windows.length) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const px = ((e.clientX - rect.left) / rect.width) * w;
    const frac = (px - PAD.left) / geom.plotW;
    const i = Math.round(
      Math.min(1, Math.max(0, frac)) * (geom.n > 1 ? geom.n - 1 : 0),
    );
    setHover(i);
  }

  const hv = hover != null ? windows[hover] : null;

  return (
    <div ref={ref} className="relative w-full">
      <div className="mb-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
        {axis === "none" ? (
          <>
            <LegendSwatch color="var(--color-brass)" label="all names" line />
            <span
              className="flex items-center gap-1.5"
              title="5th–95th percentile of the window's mean excess from resampling its stocks."
            >
              <span className="inline-block h-2.5 w-4 rounded-[1px] bg-brass/20" />
              <span className="text-muted">95% CI band</span>
            </span>
          </>
        ) : (
          eraSeries.map((s) => (
            <LegendSwatch key={s.key} color={s.color} label={s.key} line />
          ))
        )}
        <span className="ml-auto text-muted">
          excess vs {benchmark}, by formation date
        </span>
      </div>

      <svg
        width={w}
        height={H}
        role="img"
        aria-label="Mean excess return of each rolling cohort versus the benchmark, over time"
        onMouseMove={onMove}
        onMouseLeave={() => setHover(null)}
      >
        {/* zero line */}
        <line
          x1={PAD.left}
          x2={w - PAD.right}
          y1={geom.y(0)}
          y2={geom.y(0)}
          stroke="var(--color-hairline)"
          strokeDasharray="3 3"
          opacity={0.9}
        />
        <text
          x={PAD.left - 6}
          y={geom.y(0) + 3}
          textAnchor="end"
          className="num"
          fontSize={10}
          fill="var(--color-muted)"
        >
          0
        </text>
        {/* domain-end y labels */}
        {[geom.hi, geom.lo].map((v, k) => (
          <text
            key={k}
            x={PAD.left - 6}
            y={geom.y(v) + (k === 0 ? 8 : -2)}
            textAnchor="end"
            className="num"
            fontSize={10}
            fill="var(--color-muted)"
          >
            {v > 0 ? "+" : ""}
            {v.toFixed(0)}%
          </text>
        ))}

        {/* x date ticks */}
        {xTicks.map((i) => (
          <text
            key={i}
            x={geom.x(i)}
            y={H - 8}
            textAnchor={i === 0 ? "start" : i === geom.n - 1 ? "end" : "middle"}
            fontSize={10}
            fill="var(--color-muted)"
            className="num"
          >
            {shortDate(windows[i].date)}
          </text>
        ))}

        {showBand && bandPath && (
          <path d={bandPath} fill="var(--color-brass)" fillOpacity={0.14} stroke="none" />
        )}

        {axis === "none" ? (
          <path d={overallPath} fill="none" stroke="var(--color-brass)" strokeWidth={2} />
        ) : (
          eraSeries.map((s) => (
            <path
              key={s.key}
              d={line(s.values)}
              fill="none"
              stroke={s.color}
              strokeWidth={2}
            />
          ))
        )}

        {/* point markers on the active series */}
        {axis === "none"
          ? windows.map((win, i) =>
              win.mean_excess == null ? null : (
                <circle
                  key={i}
                  cx={geom.x(i)}
                  cy={geom.y(win.mean_excess)}
                  r={2.5}
                  fill="var(--color-brass)"
                />
              ),
            )
          : null}

        {hv && (
          <line
            x1={geom.x(hover!)}
            x2={geom.x(hover!)}
            y1={PAD.top}
            y2={H - PAD.bottom}
            stroke="var(--color-brass)"
            strokeDasharray="3 3"
            opacity={0.6}
          />
        )}
      </svg>

      {hv && (
        <div
          className="pointer-events-none absolute top-8 rounded-sm border border-hairline bg-ink/95 px-2.5 py-1.5 text-[11px] shadow-lg"
          style={{ left: Math.min(Math.max(geom.x(hover!) + 8, PAD.left), w - 170) }}
        >
          <div className="num mb-0.5 text-muted">{shortDate(hv.date)}</div>
          {axis === "none" ? (
            <div
              className="num flex items-center gap-1.5"
              title={ciRange(hv.mean_excess_ci)}
            >
              <span className="inline-block h-2 w-2 rounded-full bg-brass" />
              <span style={{ color: signColor(hv.mean_excess) }}>
                {pct(hv.mean_excess)}
              </span>
              <span className="text-muted">excess</span>
            </div>
          ) : (
            eraSeries.map((s) => (
              <div key={s.key} className="num flex items-center gap-1.5" title={ciRange(s.cis[hover!])}>
                <span
                  className="inline-block h-2 w-2 rounded-full"
                  style={{ backgroundColor: s.color }}
                />
                <span style={{ color: signColor(s.values[hover!]) }}>
                  {s.values[hover!] == null ? DASH : pct(s.values[hover!])}
                </span>
                <span className="text-muted">{s.key}</span>
              </div>
            ))
          )}
          <div className="num mt-0.5 text-muted">{hv.n} measured · {hv.n_cohort} in cohort</div>
        </div>
      )}
    </div>
  );
}

// ── IC-decay chart ────────────────────────────────────────────────

function IcDecay({ points }: { points: VintageStudy["ic_decay"] }) {
  const [ref, w] = useWidth();
  const [hover, setHover] = useState<number | null>(null);

  const usable = points.filter((p) => p.ic != null);
  const geom = useMemo(() => {
    const n = points.length;
    const vals: number[] = [0];
    for (const p of points) {
      if (p.ic != null) vals.push(p.ic);
      if (p.ic_ci) vals.push(p.ic_ci.ci_low, p.ic_ci.ci_high);
    }
    let lo = Math.min(...vals, -0.2);
    let hi = Math.max(...vals, 0.2);
    const padV = (hi - lo) * 0.1;
    lo -= padV;
    hi += padV;
    const plotW = Math.max(1, w - PAD.left - PAD.right);
    const plotH = H - PAD.top - PAD.bottom;
    const x = (i: number) =>
      PAD.left + (n > 1 ? (i / (n - 1)) * plotW : plotW / 2);
    const y = (v: number) => PAD.top + (1 - (v - lo) / (hi - lo)) * plotH;
    return { n, lo, hi, x, y, plotW };
  }, [points, w]);

  const linePath = useMemo(() => {
    let d = "";
    let pen = false;
    points.forEach((p, i) => {
      if (p.ic == null) {
        pen = false;
        return;
      }
      d += `${pen ? "L" : "M"}${geom.x(i).toFixed(1)} ${geom.y(p.ic).toFixed(1)} `;
      pen = true;
    });
    return d.trim();
  }, [points, geom]);

  function onMove(e: ReactMouseEvent<SVGSVGElement>) {
    if (!points.length) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const px = ((e.clientX - rect.left) / rect.width) * w;
    const frac = (px - PAD.left) / geom.plotW;
    const i = Math.round(
      Math.min(1, Math.max(0, frac)) * (geom.n > 1 ? geom.n - 1 : 0),
    );
    setHover(i);
  }

  if (!usable.length) {
    return (
      <div className="py-12 text-center text-sm text-muted">
        Not enough windows yet to estimate how the signal decays.
      </div>
    );
  }

  const hv = hover != null ? points[hover] : null;

  return (
    <div ref={ref} className="relative w-full">
      <svg
        width={w}
        height={H}
        role="img"
        aria-label="Rank correlation between score and forward return at each horizon"
        onMouseMove={onMove}
        onMouseLeave={() => setHover(null)}
      >
        {/* zero line */}
        <line
          x1={PAD.left}
          x2={w - PAD.right}
          y1={geom.y(0)}
          y2={geom.y(0)}
          stroke="var(--color-hairline)"
          strokeDasharray="3 3"
          opacity={0.9}
        />
        {[geom.hi, 0, geom.lo].map((v, k) => (
          <text
            key={k}
            x={PAD.left - 6}
            y={geom.y(v) + 3}
            textAnchor="end"
            className="num"
            fontSize={10}
            fill="var(--color-muted)"
          >
            {v.toFixed(2)}
          </text>
        ))}

        {/* CI whiskers */}
        {points.map((p, i) =>
          p.ic_ci ? (
            <line
              key={`ci${i}`}
              x1={geom.x(i)}
              x2={geom.x(i)}
              y1={geom.y(p.ic_ci.ci_low)}
              y2={geom.y(p.ic_ci.ci_high)}
              stroke="var(--color-brass)"
              strokeWidth={1.5}
              opacity={0.5}
            />
          ) : null,
        )}

        <path d={linePath} fill="none" stroke="var(--color-brass)" strokeWidth={2} />

        {points.map((p, i) =>
          p.ic == null ? null : (
            <circle
              key={i}
              cx={geom.x(i)}
              cy={geom.y(p.ic)}
              r={3}
              fill="var(--color-brass)"
            />
          ),
        )}

        {/* x horizon labels */}
        {points.map((p, i) => (
          <text
            key={`x${i}`}
            x={geom.x(i)}
            y={H - 8}
            textAnchor="middle"
            fontSize={10}
            fill="var(--color-muted)"
          >
            {horizonLabel(p.horizon)}
          </text>
        ))}

        {hv && (
          <line
            x1={geom.x(hover!)}
            x2={geom.x(hover!)}
            y1={PAD.top}
            y2={H - PAD.bottom}
            stroke="var(--color-brass)"
            strokeDasharray="3 3"
            opacity={0.6}
          />
        )}
      </svg>

      {hv && (
        <div
          className="pointer-events-none absolute top-2 rounded-sm border border-hairline bg-ink/95 px-2.5 py-1.5 text-[11px] shadow-lg"
          style={{ left: Math.min(Math.max(geom.x(hover!) + 8, PAD.left), w - 170) }}
        >
          <div className="num mb-0.5 text-muted">
            {horizonLabel(hv.horizon)} out
          </div>
          <div className="num" title={ciRange(hv.ic_ci)}>
            <span style={{ color: signColor(hv.ic) }}>
              {hv.ic == null ? DASH : hv.ic.toFixed(3)}
            </span>{" "}
            <span className="text-muted">rank corr</span>
          </div>
          <div className="num mt-0.5 text-muted">
            averaged over {hv.n_windows}{" "}
            {hv.n_windows === 1 ? "window" : "windows"}
          </div>
        </div>
      )}
    </div>
  );
}

// ── legend swatch ─────────────────────────────────────────────────

function LegendSwatch({
  color,
  label,
  line = false,
}: {
  color: string;
  label: string;
  line?: boolean;
}) {
  return (
    <span className="flex items-center gap-1.5">
      <span
        className={line ? "inline-block h-[3px] w-4 rounded-full" : "inline-block h-2.5 w-2.5 rounded-full"}
        style={{ backgroundColor: color }}
      />
      <span className="text-paper/80">{label}</span>
    </span>
  );
}

// ── the section ───────────────────────────────────────────────────

export default function VintageSignals({
  data,
  benchmark,
}: {
  data: VintageStudy;
  benchmark: string;
}) {
  const [axis, setAxis] = useState<EraAxis>("none");
  const windows = data.windows;

  if (!windows.length) {
    return (
      <Panel>
        <PanelHeader title="Signal over time" editorial />
        <p className="px-4 py-12 text-center text-sm text-muted">
          Not enough forward history yet to roll the study forward — one full
          hold horizon ({data.hold_days} trading days) must clear before the
          first window can be measured.
        </p>
      </Panel>
    );
  }

  const holdLabel = horizonLabel(data.hold_days);

  return (
    <div className="space-y-5">
      <Panel>
        <PanelHeader
          title="Signal over time"
          editorial
          hint={
            <span title={`Each point is a fresh cohort formed on that date from the then-latest scores, held ${data.hold_days} trading days.`}>
              excess vs benchmark, {holdLabel} hold, every {data.step_days}{" "}
              trading days
            </span>
          }
          right={
            <div className="inline-flex overflow-hidden rounded-sm border border-hairline">
              {(
                [
                  { key: "none", label: "All names" },
                  { key: "prompt_version", label: "By prompt version" },
                  { key: "model", label: "By model" },
                ] as { key: EraAxis; label: string }[]
              ).map((o) => {
                const active = o.key === axis;
                return (
                  <button
                    key={o.key}
                    onClick={() => setAxis(o.key)}
                    aria-pressed={active}
                    className={`px-2 py-1 text-[11px] font-medium transition-colors ${
                      active ? "bg-brass/15 text-brass" : "text-muted hover:text-paper"
                    }`}
                  >
                    {o.label}
                  </button>
                );
              })}
            </div>
          }
        />
        <div className="p-4">
          <ExcessOverTime windows={windows} axis={axis} benchmark={benchmark} />
          <p className="mt-2 text-[11px] leading-relaxed text-muted">
            Positive means the cohort beat {benchmark} over the {holdLabel} after
            each formation date. The band is a bootstrap 95% interval;{" "}
            {axis === "none"
              ? "split the line by prompt version or model to see whether a newer era moves the signal."
              : `an era with fewer than ${data.min_era_n} measured names in a window is dropped from that point rather than shown as a number.`}
          </p>
        </div>
      </Panel>

      <Panel>
        <PanelHeader
          title="How fast the signal fades"
          hint="rank correlation of score vs forward return, by horizon"
        />
        <div className="p-4">
          <p className="mb-3 text-xs text-muted">
            The same windows, measured at several horizons. A line that slopes
            down means the score&apos;s ordering of winners loses its edge as the
            holding period lengthens. Averaged across{" "}
            {data.window_dates.length}{" "}
            {data.window_dates.length === 1 ? "window" : "windows"}, with
            bootstrap intervals over the windows.
          </p>
          <IcDecay points={data.ic_decay} />
        </div>
      </Panel>
    </div>
  );
}
