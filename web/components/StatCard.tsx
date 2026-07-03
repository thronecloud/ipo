import type { ReactNode } from "react";

// Compact terminal stat: label over a mono value, hairline-boxed.
// Deliberately NOT a big-number hero card — quiet, dense, tabular.
export default function StatCard({
  label,
  value,
  sub,
  accent,
  icon,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  accent?: string;
  icon?: ReactNode;
}) {
  return (
    <div className="rounded-md border border-hairline bg-panel px-3.5 py-3">
      <div className="flex items-center justify-between">
        <span className="text-[11px] font-medium uppercase tracking-[0.12em] text-muted">
          {label}
        </span>
        {icon}
      </div>
      <div
        className="num mt-1.5 text-2xl font-semibold leading-none"
        style={{ color: accent ?? "var(--color-paper)" }}
      >
        {value}
      </div>
      {sub && <div className="mt-1 text-xs text-muted">{sub}</div>}
    </div>
  );
}
