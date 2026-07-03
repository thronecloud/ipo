// The Council — fixed canonical order. This ordering is load-bearing:
// every ConvictionStrip renders its 10 cells in exactly this sequence.
import type { Recommendation } from "./types";

export interface Persona {
  slug: string;
  name: string;
  short: string; // surname / handle for compact tooltips
  nationality: "international" | "indian";
}

export const PERSONA_ORDER: Persona[] = [
  { slug: "warren_buffett", name: "Warren Buffett", short: "Buffett", nationality: "international" },
  { slug: "charlie_munger", name: "Charlie Munger", short: "Munger", nationality: "international" },
  { slug: "benjamin_graham", name: "Benjamin Graham", short: "Graham", nationality: "international" },
  { slug: "peter_lynch", name: "Peter Lynch", short: "Lynch", nationality: "international" },
  { slug: "philip_fisher", name: "Philip Fisher", short: "Fisher", nationality: "international" },
  { slug: "joel_greenblatt", name: "Joel Greenblatt", short: "Greenblatt", nationality: "international" },
  { slug: "howard_marks", name: "Howard Marks", short: "Marks", nationality: "international" },
  { slug: "rakesh_jhunjhunwala", name: "Rakesh Jhunjhunwala", short: "Jhunjhunwala", nationality: "indian" },
  { slug: "radhakishan_damani", name: "Radhakishan Damani", short: "Damani", nationality: "indian" },
  { slug: "vijay_kedia", name: "Vijay Kedia", short: "Kedia", nationality: "indian" },
];

export const PERSONA_SLUGS = PERSONA_ORDER.map((p) => p.slug);

export const PERSONA_BY_SLUG: Record<string, Persona> = Object.fromEntries(
  PERSONA_ORDER.map((p) => [p.slug, p]),
);

// two-letter monogram for portrait chips
export function monogram(name: string): string {
  const parts = name.trim().split(/\s+/);
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

// recommendation → token color (used by strip cells, chips, bars)
export const REC_COLOR: Record<Recommendation, string> = {
  BUY: "var(--color-sage)",
  HOLD: "var(--color-brass)",
  AVOID: "var(--color-terracotta)",
};

export const REC_TW: Record<Recommendation, string> = {
  BUY: "text-sage",
  HOLD: "text-brass",
  AVOID: "text-terracotta",
};
