"use client";

import { useState } from "react";
import type { CouncilVerdict } from "@/lib/types";
import { PERSONA_BY_SLUG, monogram } from "@/lib/personas";
import { score10, score10Color, shortDate } from "@/lib/format";
import RecChip from "./RecChip";

export default function CouncilBlock({ verdict }: { verdict: CouncilVerdict }) {
  const [open, setOpen] = useState(false);
  const persona = PERSONA_BY_SLUG[verdict.persona];
  const analyzed = verdict.recommendation !== null || verdict.score !== null;
  const color = score10Color(verdict.score);

  return (
    <article
      className={`rounded-md border bg-panel p-4 ${analyzed ? "border-hairline" : "border-dashed border-hairline/70"}`}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-3">
          <div
            className="num flex h-9 w-9 shrink-0 items-center justify-center rounded-sm border text-xs font-semibold"
            style={{
              borderColor: analyzed ? color : "var(--color-hairline)",
              color: analyzed ? color : "var(--color-muted)",
              background: "var(--color-panel2)",
            }}
            aria-hidden
          >
            {monogram(verdict.display_name)}
          </div>
          <div>
            <h3 className="serif text-base font-medium leading-tight text-paper">
              {verdict.display_name}
            </h3>
            <span className="text-[10px] uppercase tracking-[0.14em] text-muted">
              {persona?.nationality ?? verdict.nationality}
            </span>
          </div>
        </div>
        {analyzed ? (
          <div className="flex flex-col items-end gap-1">
            <span className="num text-lg font-semibold leading-none" style={{ color }}>
              {score10(verdict.score)}
              <span className="text-[11px] text-muted">/10</span>
            </span>
            <RecChip rec={verdict.recommendation} size="xs" />
          </div>
        ) : (
          <span className="text-[10px] uppercase tracking-[0.14em] text-muted">
            no verdict
          </span>
        )}
      </div>

      {!analyzed ? (
        <p className="wisdom mt-3 text-sm text-muted">
          Awaiting the council. This name has not yet been put before {verdict.display_name}.
        </p>
      ) : (
        <>
          {verdict.investment_thesis && (
            <blockquote className="wisdom mt-3 border-l-2 border-hairline pl-3 text-[15px] leading-relaxed text-paper/90">
              {verdict.investment_thesis}
            </blockquote>
          )}

          {(verdict.key_strengths.length > 0 || verdict.key_risks.length > 0) && (
            <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
              <PointList
                title="Strengths"
                items={verdict.key_strengths}
                dot="var(--color-sage)"
              />
              <PointList
                title="Risks"
                items={verdict.key_risks}
                dot="var(--color-terracotta)"
              />
            </div>
          )}

          {verdict.red_flags.length > 0 && (
            <div className="mt-3 rounded-sm border border-terracotta/30 bg-terracotta/5 p-2.5">
              <span className="text-[10px] font-semibold uppercase tracking-[0.14em] text-terracotta">
                Red flags
              </span>
              <ul className="mt-1 space-y-1">
                {verdict.red_flags.map((f, i) => (
                  <li key={i} className="text-xs text-paper/90">
                    {f}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {verdict.detailed_analysis && (
            <div className="mt-3">
              <button
                onClick={() => setOpen((o) => !o)}
                aria-expanded={open}
                className="text-xs font-medium text-brass hover:underline"
              >
                {open ? "Hide" : "Read"} detailed analysis
              </button>
              {open && (
                <p className="mt-2 whitespace-pre-wrap text-sm leading-relaxed text-paper/85">
                  {verdict.detailed_analysis}
                </p>
              )}
            </div>
          )}

          <div className="mt-3 flex items-center gap-3 border-t border-hairline/60 pt-2 text-[10px] text-muted">
            {verdict.model && <span className="num">{verdict.model}</span>}
            {verdict.analyzed_at && <span>· {shortDate(verdict.analyzed_at)}</span>}
          </div>
        </>
      )}
    </article>
  );
}

function PointList({
  title,
  items,
  dot,
}: {
  title: string;
  items: string[];
  dot: string;
}) {
  if (items.length === 0)
    return (
      <div>
        <span className="text-[10px] font-semibold uppercase tracking-[0.14em] text-muted">
          {title}
        </span>
        <p className="mt-1 text-xs text-muted">—</p>
      </div>
    );
  return (
    <div>
      <span className="text-[10px] font-semibold uppercase tracking-[0.14em] text-muted">
        {title}
      </span>
      <ul className="mt-1 space-y-1">
        {items.map((s, i) => (
          <li key={i} className="flex gap-1.5 text-xs leading-snug text-paper/85">
            <span
              className="mt-1 inline-block h-1 w-1 shrink-0 rounded-full"
              style={{ background: dot }}
              aria-hidden
            />
            {s}
          </li>
        ))}
      </ul>
    </div>
  );
}
