"use client";

// Lightweight SVG-free chart primitives — bars, stacked bars, histograms and a
// sparkline. No chart lib; everything is divs/SVG so it stays crisp and small.

export function StackedBar({
  segments,
  total,
  height = 8,
}: {
  segments: { value: number; color: string; label?: string }[];
  total: number;
  height?: number;
}) {
  const denom = total > 0 ? total : 1;
  return (
    <div
      className="flex w-full overflow-hidden rounded-sm bg-hairline/40"
      style={{ height }}
      role="img"
      aria-label={segments.map((s) => `${s.label ?? ""} ${s.value}`).join(", ")}
    >
      {segments.map((s, i) => (
        <div
          key={i}
          style={{
            width: `${(s.value / denom) * 100}%`,
            background: s.color,
          }}
          title={`${s.label ?? ""}: ${s.value}`}
        />
      ))}
    </div>
  );
}

export function MiniBars({
  values,
  color = "var(--color-brass)",
  height = 28,
  labels,
}: {
  values: number[];
  color?: string;
  height?: number;
  labels?: string[];
}) {
  const max = Math.max(1, ...values);
  return (
    <div className="flex items-end gap-0.5" style={{ height }}>
      {values.map((v, i) => (
        <div
          key={i}
          className="flex-1 rounded-[1px] transition-[height]"
          style={{
            height: `${Math.max(2, (v / max) * height)}px`,
            background: v === 0 ? "var(--color-hairline)" : color,
            minWidth: 3,
          }}
          title={labels ? `${labels[i]}: ${v}` : String(v)}
        />
      ))}
    </div>
  );
}

// Horizontal labeled meter (e.g. coverage progress).
export function Meter({
  value,
  max,
  color = "var(--color-sage)",
  height = 6,
}: {
  value: number;
  max: number;
  color?: string;
  height?: number;
}) {
  const pct = max > 0 ? Math.min(100, (value / max) * 100) : 0;
  return (
    <div
      className="relative overflow-hidden rounded-full bg-hairline"
      style={{ height, width: "100%" }}
    >
      <div
        className="absolute inset-y-0 left-0 rounded-full"
        style={{ width: `${pct}%`, background: color }}
      />
    </div>
  );
}

// Sparkline / line chart for conviction history. Handles a single point.
export function Sparkline({
  points,
  width = 640,
  height = 140,
  color = "var(--color-brass)",
  min = 0,
  max = 100,
}: {
  points: { x: number; y: number | null; label?: string }[];
  width?: number;
  height?: number;
  color?: string;
  min?: number;
  max?: number;
}) {
  const valid = points.filter((p) => p.y !== null) as {
    x: number;
    y: number;
    label?: string;
  }[];
  if (valid.length === 0) return null;

  const pad = 8;
  const w = width - pad * 2;
  const h = height - pad * 2;
  const n = valid.length;
  const xFor = (i: number) => pad + (n === 1 ? w / 2 : (i / (n - 1)) * w);
  const yFor = (v: number) =>
    pad + h - ((v - min) / (max - min || 1)) * h;

  const path = valid
    .map((p, i) => `${i === 0 ? "M" : "L"} ${xFor(i).toFixed(1)} ${yFor(p.y).toFixed(1)}`)
    .join(" ");

  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      className="w-full"
      style={{ height }}
      preserveAspectRatio="none"
      role="img"
      aria-label="Composite score over time"
    >
      {[0.25, 0.5, 0.75].map((g) => (
        <line
          key={g}
          x1={pad}
          x2={width - pad}
          y1={pad + h * g}
          y2={pad + h * g}
          stroke="var(--color-hairline)"
          strokeWidth={1}
        />
      ))}
      {n > 1 && (
        <path d={path} fill="none" stroke={color} strokeWidth={1.5} />
      )}
      {valid.map((p, i) => (
        <circle
          key={i}
          cx={xFor(i)}
          cy={yFor(p.y)}
          r={n === 1 ? 4 : 2.5}
          fill={color}
        />
      ))}
    </svg>
  );
}
