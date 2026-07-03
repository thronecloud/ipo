"use client";

import type { CouncilVerdict } from "@/lib/types";
import { buildDebate } from "@/lib/compute";
import { score10Color, score10 } from "@/lib/format";

function SideHead({
  label,
  count,
  color,
}: {
  label: string;
  count: number;
  color: string;
}) {
  return (
    <div className="flex items-baseline justify-between">
      <h3 className="serif text-lg font-medium" style={{ color }}>
        {label}
      </h3>
      <span className="num text-xs text-muted">
        {count} {count === 1 ? "voice" : "voices"}
      </span>
    </div>
  );
}

function Members({ members }: { members: CouncilVerdict[] }) {
  if (members.length === 0)
    return <p className="mt-2 text-xs text-muted">No one in this camp.</p>;
  return (
    <div className="mt-2 flex flex-wrap gap-1.5">
      {members.map((m) => (
        <span
          key={m.persona}
          className="inline-flex items-center gap-1 rounded-sm border border-hairline bg-panel2 px-1.5 py-0.5 text-[11px] text-paper/90"
        >
          {m.display_name}
          <span className="num" style={{ color: score10Color(m.score) }}>
            {score10(m.score)}
          </span>
        </span>
      ))}
    </div>
  );
}

function Points({
  points,
  color,
}: {
  points: { text: string; by: string[] }[];
  color: string;
}) {
  if (points.length === 0) return null;
  return (
    <ul className="mt-3 space-y-1.5">
      {points.map((p, i) => (
        <li key={i} className="flex items-start gap-2 text-sm leading-snug">
          <span
            className="mt-1.5 inline-block h-1 w-1 shrink-0 rounded-full"
            style={{ background: color }}
            aria-hidden
          />
          <span className="text-paper/90">
            {p.text}
            {p.by.length > 1 && (
              <span className="num ml-1.5 text-[10px] text-muted">
                ×{p.by.length}
              </span>
            )}
          </span>
        </li>
      ))}
    </ul>
  );
}

export default function DebatePanel({ council }: { council: CouncilVerdict[] }) {
  const debate = buildDebate(council);
  const analyzedCount =
    debate.bulls.personas.length +
    debate.bears.personas.length +
    debate.neutral.length;

  if (analyzedCount === 0) {
    return (
      <p className="px-4 py-8 text-center text-sm text-muted">
        The debate opens once the council has weighed in.
      </p>
    );
  }

  return (
    <div className="grid grid-cols-1 gap-px bg-hairline md:grid-cols-2">
      <div className="bg-panel p-4">
        <SideHead
          label="The Bulls"
          count={debate.bulls.personas.length}
          color="var(--color-sage)"
        />
        <Members members={debate.bulls.personas} />
        <Points points={debate.bulls.points} color="var(--color-sage)" />
      </div>
      <div className="bg-panel p-4">
        <SideHead
          label="The Bears"
          count={debate.bears.personas.length}
          color="var(--color-terracotta)"
        />
        <Members members={debate.bears.personas} />
        <Points points={debate.bears.points} color="var(--color-terracotta)" />
      </div>
      {debate.neutral.length > 0 && (
        <div className="bg-panel p-4 md:col-span-2">
          <SideHead
            label="On the Fence"
            count={debate.neutral.length}
            color="var(--color-brass)"
          />
          <Members members={debate.neutral} />
        </div>
      )}
    </div>
  );
}
