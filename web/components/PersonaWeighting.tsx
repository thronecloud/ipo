"use client";

import { PERSONA_ORDER } from "@/lib/personas";

// Choose which of the 10 council members count toward the composite. When a
// subset is active, the ranked table recomputes each row's score client-side.
export default function PersonaWeighting({
  selected,
  onChange,
}: {
  selected: string[];
  onChange: (next: string[]) => void;
}) {
  const set = new Set(selected);
  const all = selected.length >= PERSONA_ORDER.length;

  function toggle(slug: string) {
    const next = new Set(set);
    if (next.has(slug)) next.delete(slug);
    else next.add(slug);
    // keep canonical order
    onChange(PERSONA_ORDER.filter((p) => next.has(p.slug)).map((p) => p.slug));
  }

  return (
    <div>
      <div className="mb-2 flex items-center justify-between">
        <span className="text-[10px] font-semibold uppercase tracking-[0.14em] text-muted">
          Weight the council
        </span>
        <button
          onClick={() =>
            onChange(all ? [] : PERSONA_ORDER.map((p) => p.slug))
          }
          className="text-[10px] font-medium text-brass hover:underline"
        >
          {all ? "clear" : "all"}
        </button>
      </div>
      <div className="grid grid-cols-2 gap-1">
        {PERSONA_ORDER.map((p) => {
          const on = set.has(p.slug);
          return (
            <button
              key={p.slug}
              onClick={() => toggle(p.slug)}
              aria-pressed={on}
              className={`flex items-center gap-1.5 rounded-sm border px-1.5 py-1 text-left text-[11px] transition-colors ${
                on
                  ? "border-brass/40 bg-brass/10 text-paper"
                  : "border-hairline bg-panel text-muted hover:text-paper"
              }`}
            >
              <span
                className={`inline-block h-2 w-2 shrink-0 rounded-[1px] border ${
                  on ? "border-brass bg-brass" : "border-hairline"
                }`}
                aria-hidden
              />
              <span className="truncate">{p.short}</span>
            </button>
          );
        })}
      </div>
      {!all && (
        <p className="mt-2 text-[10px] leading-snug text-muted">
          Re-ranking the page by {selected.length || "no"} selected{" "}
          {selected.length === 1 ? "voice" : "voices"}.
        </p>
      )}
    </div>
  );
}
