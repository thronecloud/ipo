// Client-side recomputation of the composite from a chosen subset of the
// council. When all 10 are selected this should track the server composite
// closely; deselecting personas lets a user weight the verdict toward the
// investors they trust.

import type { PerPersonaVerdict, Recommendation, StockRow } from "./types";

export interface Recomputed {
  composite: number | null; // 0-100
  consensus: Recommendation | null;
  coverage: number; // # of selected personas that actually scored this stock
}

// Average selected persona scores (0-10) → composite (0-100).
export function recompute(
  perPersona: Record<string, PerPersonaVerdict>,
  selected: string[],
): Recomputed {
  const scores: number[] = [];
  const recs: Recommendation[] = [];
  for (const slug of selected) {
    const v = perPersona[slug];
    if (!v) continue;
    if (typeof v.score === "number") scores.push(v.score);
    if (v.recommendation) recs.push(v.recommendation);
  }
  if (scores.length === 0) {
    return { composite: null, consensus: null, coverage: 0 };
  }
  const avg = scores.reduce((a, b) => a + b, 0) / scores.length;
  const counts: Record<Recommendation, number> = { BUY: 0, HOLD: 0, AVOID: 0 };
  for (const r of recs) counts[r] += 1;
  let consensus: Recommendation | null = null;
  let best = -1;
  (["BUY", "HOLD", "AVOID"] as Recommendation[]).forEach((r) => {
    if (counts[r] > best) {
      best = counts[r];
      consensus = r;
    }
  });
  return {
    composite: Math.round(avg * 10 * 10) / 10,
    consensus: recs.length ? consensus : null,
    coverage: scores.length,
  };
}

// A row's effective composite given the current selection. If the full council
// is selected we trust the server value; otherwise we recompute locally.
export function effectiveComposite(
  row: StockRow,
  selected: string[],
  totalPersonas: number,
): Recomputed {
  if (selected.length >= totalPersonas) {
    return {
      composite: row.composite_score,
      consensus: row.consensus_recommendation,
      coverage: row.analysis_coverage,
    };
  }
  return recompute(row.per_persona, selected);
}

// ── The Debate: synthesize bulls vs bears from the council ────────
import type { CouncilVerdict } from "./types";

export interface DebateSide {
  personas: CouncilVerdict[];
  points: { text: string; by: string[] }[]; // shared themes with attribution
}

export interface Debate {
  bulls: DebateSide;
  bears: DebateSide;
  neutral: CouncilVerdict[];
}

function aggregate(
  members: CouncilVerdict[],
  pick: (c: CouncilVerdict) => string[],
): { text: string; by: string[] }[] {
  const map = new Map<string, { text: string; by: Set<string> }>();
  for (const m of members) {
    for (const raw of pick(m) ?? []) {
      const text = raw.trim();
      if (!text) continue;
      const key = text.toLowerCase().slice(0, 80);
      if (!map.has(key)) map.set(key, { text, by: new Set() });
      map.get(key)!.by.add(m.display_name);
    }
  }
  return Array.from(map.values())
    .map((v) => ({ text: v.text, by: Array.from(v.by) }))
    .sort((a, b) => b.by.length - a.by.length)
    .slice(0, 8);
}

export function buildDebate(council: CouncilVerdict[]): Debate {
  const analyzed = council.filter((c) => c.recommendation);
  const bulls = analyzed.filter((c) => c.recommendation === "BUY");
  const bears = analyzed.filter((c) => c.recommendation === "AVOID");
  const neutral = analyzed.filter((c) => c.recommendation === "HOLD");
  return {
    bulls: {
      personas: bulls,
      points: aggregate(bulls, (c) => c.key_strengths),
    },
    bears: {
      personas: bears,
      points: aggregate(bears, (c) => [...c.key_risks, ...c.red_flags]),
    },
    neutral,
  };
}
