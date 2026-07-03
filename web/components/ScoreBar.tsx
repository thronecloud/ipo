import { composite, compositeColor } from "@/lib/format";

// Thin conviction bar for a 0-100 composite, with the number in mono.
export default function ScoreBar({
  value,
  width = 56,
  showValue = true,
}: {
  value: number | null | undefined;
  width?: number;
  showValue?: boolean;
}) {
  const color = compositeColor(value);
  const pct = value === null || value === undefined ? 0 : Math.max(0, Math.min(100, value));
  return (
    <div className="flex items-center gap-2">
      {showValue && (
        <span
          className="num tabular-nums text-xs"
          style={{ color: value == null ? "var(--color-muted)" : color, minWidth: 30 }}
        >
          {composite(value)}
        </span>
      )}
      <div
        className="relative h-1 overflow-hidden rounded-full bg-hairline"
        style={{ width }}
        aria-hidden
      >
        <div
          className="absolute inset-y-0 left-0 rounded-full transition-[width]"
          style={{ width: `${pct}%`, background: color }}
        />
      </div>
    </div>
  );
}
