// Number / value formatting helpers. Numerics render with the .num class
// (IBM Plex Mono, tabular-nums) at the component level.

export const DASH = "—"; // em dash for missing values

export function num(
  value: number | null | undefined,
  opts: { decimals?: number; suffix?: string } = {},
): string {
  if (value === null || value === undefined || Number.isNaN(value)) return DASH;
  const { decimals = 2, suffix = "" } = opts;
  return (
    value.toLocaleString("en-IN", {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals,
    }) + suffix
  );
}

export function pct(value: number | null | undefined, decimals = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return DASH;
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(decimals)}%`;
}

// Indian rupee crore formatting for market cap. Values arrive in crore.
export function crore(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return DASH;
  if (value >= 100000) return `${(value / 100000).toFixed(2)}L Cr`;
  if (value >= 1000) return `${(value / 1000).toFixed(2)}K Cr`;
  return `${value.toLocaleString("en-IN", { maximumFractionDigits: 0 })} Cr`;
}

export function price(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return DASH;
  return `₹${value.toLocaleString("en-IN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

// composite score is 0-100
export function composite(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return DASH;
  return value.toFixed(1);
}

// persona score is 0-10
export function score10(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return DASH;
  return value.toFixed(1);
}

export function relTime(iso: string | null | undefined): string {
  if (!iso) return DASH;
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return DASH;
  const diff = Date.now() - then;
  const s = Math.round(diff / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.round(h / 24);
  if (d < 30) return `${d}d ago`;
  const mo = Math.round(d / 30);
  if (mo < 12) return `${mo}mo ago`;
  return `${Math.round(mo / 12)}y ago`;
}

export function shortDate(iso: string | null | undefined): string {
  if (!iso) return DASH;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return DASH;
  return d.toLocaleDateString("en-IN", {
    day: "2-digit",
    month: "short",
    year: "numeric",
  });
}

export function titleCase(s: string): string {
  return s
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

// color for a composite 0-100 score
export function compositeColor(value: number | null | undefined): string {
  if (value === null || value === undefined) return "var(--color-muted)";
  if (value >= 65) return "var(--color-sage)";
  if (value >= 45) return "var(--color-brass)";
  return "var(--color-terracotta)";
}

// color for a persona 0-10 score
export function score10Color(value: number | null | undefined): string {
  if (value === null || value === undefined) return "var(--color-muted)";
  if (value >= 7) return "var(--color-sage)";
  if (value >= 4) return "var(--color-brass)";
  return "var(--color-terracotta)";
}
