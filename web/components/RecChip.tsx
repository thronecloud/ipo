import type { Recommendation } from "@/lib/types";

const STYLES: Record<Recommendation, { fg: string; bg: string; bd: string }> = {
  BUY: { fg: "text-sage", bg: "bg-sage/10", bd: "border-sage/40" },
  HOLD: { fg: "text-brass", bg: "bg-brass/10", bd: "border-brass/40" },
  AVOID: {
    fg: "text-terracotta",
    bg: "bg-terracotta/10",
    bd: "border-terracotta/40",
  },
};

export default function RecChip({
  rec,
  size = "sm",
}: {
  rec: Recommendation | null | undefined;
  size?: "xs" | "sm";
}) {
  const pad = size === "xs" ? "px-1.5 py-0.5 text-[10px]" : "px-2 py-0.5 text-xs";
  if (!rec) {
    return (
      <span
        className={`inline-flex items-center rounded-sm border border-hairline bg-hairline/20 font-medium uppercase tracking-wide text-muted ${pad}`}
      >
        —
      </span>
    );
  }
  const s = STYLES[rec];
  return (
    <span
      className={`inline-flex items-center rounded-sm border font-semibold uppercase tracking-wide ${s.fg} ${s.bg} ${s.bd} ${pad}`}
    >
      {rec}
    </span>
  );
}
