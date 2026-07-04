import type { ConfidenceTier } from "@/lib/types";

// Confidence-tier chip — same visual grammar as RecChip.
// high = agreement across independent axes; moderate = concordant but tepid;
// mixed = the axes disagree on direction; provisional = an axis is missing.
const STYLES: Record<ConfidenceTier, { fg: string; bg: string; bd: string }> = {
  high: { fg: "text-sage", bg: "bg-sage/10", bd: "border-sage/40" },
  moderate: { fg: "text-paper/70", bg: "bg-hairline/20", bd: "border-hairline" },
  mixed: { fg: "text-brass", bg: "bg-brass/10", bd: "border-brass/40" },
  provisional: { fg: "text-muted", bg: "bg-hairline/10", bd: "border-hairline" },
};

export default function TierChip({
  tier,
  size = "sm",
}: {
  tier: ConfidenceTier | string | null | undefined;
  size?: "xs" | "sm";
}) {
  const pad = size === "xs" ? "px-1.5 py-0.5 text-[10px]" : "px-2 py-0.5 text-xs";
  const s = tier ? STYLES[tier as ConfidenceTier] : undefined;
  if (!s) {
    return (
      <span
        className={`inline-flex items-center rounded-sm border border-hairline bg-hairline/20 font-medium uppercase tracking-wide text-muted ${pad}`}
      >
        —
      </span>
    );
  }
  return (
    <span
      className={`inline-flex items-center rounded-sm border font-semibold uppercase tracking-wide ${s.fg} ${s.bg} ${s.bd} ${pad}`}
    >
      {tier}
    </span>
  );
}
