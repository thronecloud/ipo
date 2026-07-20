"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useMemo } from "react";
import { api } from "@/lib/api";
import { useAsync, useLocalStorage } from "@/lib/hooks";
import type {
  CompositeSummary,
  CouncilVerdict,
  PerPersonaVerdict,
  StockDetail,
} from "@/lib/types";
import { PERSONA_ORDER } from "@/lib/personas";
import {
  composite,
  compositeColor,
  crore,
  num,
  pct,
  price,
  relTime,
  titleCase,
  DASH,
} from "@/lib/format";
import ConvictionStrip from "@/components/ConvictionStrip";
import PriceChart from "@/components/PriceChart";
import RecChip from "@/components/RecChip";
import TierChip from "@/components/TierChip";
import CouncilBlock from "@/components/CouncilBlock";
import DebatePanel from "@/components/DebatePanel";
import Fundamentals from "@/components/Fundamentals";
import ConvictionHistory from "@/components/ConvictionHistory";
import { Panel, PanelHeader } from "@/components/Panel";
import { ErrorState, Skeleton } from "@/components/States";

function emptyVerdict(slug: string, name: string, nat: string): CouncilVerdict {
  return {
    persona: slug,
    display_name: name,
    nationality: nat,
    score: null,
    recommendation: null,
    investment_thesis: null,
    key_strengths: [],
    key_risks: [],
    red_flags: [],
    detailed_analysis: null,
    metrics_evaluated: null,
    model: null,
    analyzed_at: null,
  };
}

const DQ_LABEL: Record<string, string> = {
  full: "Full data",
  partial: "Partial data",
  limited: "Limited data",
  minimal: "Minimal data",
};

export default function StockPage() {
  const params = useParams<{ symbol: string }>();
  const symbol = decodeURIComponent(
    Array.isArray(params.symbol) ? params.symbol[0] : params.symbol,
  );

  const fetcher = useCallback(
    (s: AbortSignal) => api.stock(symbol, s),
    [symbol],
  );
  const { data, loading, error, refetch } = useAsync<StockDetail>(fetcher, [
    symbol,
  ]);

  const [watchlist, setWatchlist] = useLocalStorage<string[]>("wi.watchlist", []);
  const watched = watchlist.includes(symbol);

  // Full council in canonical order, back-filling unanalyzed personas.
  const fullCouncil = useMemo<CouncilVerdict[]>(() => {
    const bySlug = new Map((data?.council ?? []).map((c) => [c.persona, c]));
    return PERSONA_ORDER.map(
      (p) =>
        bySlug.get(p.slug) ?? emptyVerdict(p.slug, p.name, p.nationality),
    );
  }, [data]);

  const perPersona = useMemo<Record<string, PerPersonaVerdict>>(() => {
    const map: Record<string, PerPersonaVerdict> = {};
    for (const c of fullCouncil) {
      map[c.persona] = { score: c.score, recommendation: c.recommendation };
    }
    return map;
  }, [fullCouncil]);

  if (error) {
    return (
      <div className="mx-auto max-w-2xl py-10">
        <BackLink />
        <div className="mt-4 rounded-md border border-hairline bg-panel">
          <ErrorState message={error} onRetry={refetch} />
        </div>
      </div>
    );
  }

  if (loading && !data) {
    return <StockSkeleton />;
  }

  if (!data) return null;

  const { identity, quote, composite: comp } = data;
  const analyzed = comp.composite_score !== null;

  return (
    <div className="space-y-5">
      <BackLink />

      {/* ── Hero ─────────────────────────────────────────────── */}
      <Panel className="overflow-hidden">
        <div className="grid grid-cols-1 gap-5 p-5 lg:grid-cols-[minmax(0,1fr)_auto]">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              {identity.universe.map((u) => (
                <span
                  key={u}
                  className="rounded-sm border border-hairline px-1.5 py-0.5 text-[10px] uppercase tracking-[0.12em] text-muted"
                >
                  {titleCase(u)}
                </span>
              ))}
              {quote.data_quality && (
                <span className="rounded-sm border border-brass/30 bg-brass/10 px-1.5 py-0.5 text-[10px] uppercase tracking-[0.12em] text-brass">
                  {DQ_LABEL[quote.data_quality] ?? quote.data_quality}
                </span>
              )}
            </div>

            <h1 className="serif mt-2 text-3xl font-semibold leading-tight text-paper">
              {identity.company_name}
            </h1>
            <div className="num mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-muted">
              <span className="font-medium text-paper">{identity.symbol}</span>
              {identity.exchange && <span>· {identity.exchange}</span>}
              {identity.sector && <span>· {identity.sector}</span>}
              {identity.cap_category && (
                <span>· {titleCase(identity.cap_category)} cap</span>
              )}
            </div>

            <div className="mt-4 flex flex-wrap gap-x-6 gap-y-3">
              <Stat label="Price" value={price(quote.current_price)} />
              <Stat label="Market cap" value={crore(quote.market_cap_cr)} />
              <Stat
                label="P/E"
                value={quote.pe_ratio == null ? DASH : num(quote.pe_ratio, { decimals: 1 })}
              />
              <Stat
                label="ROE"
                value={quote.roe == null ? DASH : pct(quote.roe)}
              />
              <Stat
                label="D/E"
                value={
                  quote.debt_to_equity == null
                    ? DASH
                    : num(quote.debt_to_equity, { decimals: 2, suffix: "x" })
                }
              />
              {quote.ipo_return_pct != null && (
                <Stat
                  label="IPO return"
                  value={pct(quote.ipo_return_pct)}
                  color={
                    quote.ipo_return_pct >= 0
                      ? "var(--color-sage)"
                      : "var(--color-terracotta)"
                  }
                />
              )}
            </div>
          </div>

          {/* verdict block */}
          <div className="flex flex-col items-start gap-3 border-hairline lg:min-w-[260px] lg:border-l lg:pl-5">
            <div className="flex items-center justify-between gap-4 self-stretch">
              <span className="text-[10px] font-semibold uppercase tracking-[0.16em] text-muted">
                Composite conviction
              </span>
              <button
                onClick={() =>
                  setWatchlist((w) =>
                    w.includes(symbol)
                      ? w.filter((s) => s !== symbol)
                      : [...w, symbol],
                  )
                }
                aria-pressed={watched}
                className={`inline-flex items-center gap-1 rounded-sm border px-2 py-1 text-[11px] font-medium transition-colors ${
                  watched
                    ? "border-brass/50 bg-brass/10 text-brass"
                    : "border-hairline text-muted hover:text-paper"
                }`}
              >
                <svg width="12" height="12" viewBox="0 0 24 24" fill={watched ? "currentColor" : "none"} stroke="currentColor" strokeWidth="1.5">
                  <path d="m12 3 2.6 5.3 5.9.9-4.3 4.1 1 5.8L12 16.9 6.8 19.7l1-5.8L3.5 9.8l5.9-.9Z" strokeLinejoin="round" />
                </svg>
                {watched ? "Watching" : "Watch"}
              </button>
            </div>

            {analyzed ? (
              <div className="flex items-end gap-3">
                <span
                  className="num text-5xl font-semibold leading-none"
                  style={{ color: compositeColor(comp.composite_score) }}
                >
                  {composite(comp.composite_score)}
                </span>
                <span className="num mb-1 text-sm text-muted">/100</span>
              </div>
            ) : (
              <div className="wisdom text-lg text-muted">Not yet analyzed</div>
            )}

            <div className="flex items-center gap-2">
              <RecChip rec={comp.consensus_recommendation} />
              <span className="num text-xs text-muted">
                {comp.analysis_coverage}/{comp.total_personas} voices
              </span>
            </div>

            {analyzed && comp.updated_at && (
              <span
                className="num text-[11px] text-muted"
                title={new Date(comp.updated_at).toLocaleString("en-IN")}
              >
                Last analyzed {relTime(comp.updated_at)}
              </span>
            )}

            <div className="mt-1">
              <ConvictionStrip perPersona={perPersona} size="lg" labeled />
            </div>

            {analyzed && (
              <div className="num flex gap-3 text-[11px] text-muted">
                {(["BUY", "HOLD", "AVOID"] as const).map((r) => (
                  <span key={r}>
                    <span
                      style={{
                        color:
                          r === "BUY"
                            ? "var(--color-sage)"
                            : r === "HOLD"
                              ? "var(--color-brass)"
                              : "var(--color-terracotta)",
                      }}
                    >
                      {comp.recommendation_counts?.[r] ?? 0}
                    </span>{" "}
                    {r}
                  </span>
                ))}
              </div>
            )}

            {analyzed && (comp.confidence_tier || comp.lcb != null) && (
              <ConfidenceBlock comp={comp} />
            )}
          </div>
        </div>
      </Panel>

      {/* ── Price ────────────────────────────────────────────── */}
      <PriceChart symbol={identity.symbol} />

      {/* ── The Council ──────────────────────────────────────── */}
      <Panel>
        <PanelHeader
          title="The Council"
          editorial
          hint="ten legendary investors, in fixed order"
          right={
            <span className="num text-xs text-muted">
              {comp.analysis_coverage} of {comp.total_personas} analyzed
            </span>
          }
        />
        <div className="grid grid-cols-1 gap-3 p-4 md:grid-cols-2">
          {fullCouncil.map((v) => (
            <CouncilBlock key={v.persona} verdict={v} />
          ))}
        </div>
      </Panel>

      {/* ── The Debate ───────────────────────────────────────── */}
      <Panel>
        <PanelHeader
          title="The Debate"
          editorial
          hint="bulls versus bears, synthesized from the council"
        />
        <DebatePanel council={fullCouncil} />
      </Panel>

      {/* ── Fundamentals ─────────────────────────────────────── */}
      <Panel>
        <PanelHeader title="Fundamentals" editorial />
        <div className="p-4">
          <Fundamentals data={data.fundamentals} />
        </div>
      </Panel>

      {/* ── Conviction over time ─────────────────────────────── */}
      <Panel>
        <PanelHeader title="Conviction over time" editorial />
        <ConvictionHistory history={data.conviction_history} />
      </Panel>
    </div>
  );
}

// The confidence layer (LEAK#1): tier, LCB and the 4 independent axes.
const AXIS_ORDER: { key: string; label: string }[] = [
  { key: "core", label: "Core" },
  { key: "growth", label: "Growth" },
  { key: "value", label: "Value" },
  { key: "independent", label: "Indep" },
];

const TIER_CAPTION: Record<string, string> = {
  provisional:
    "Provisional — at least one scoring axis has no voice yet, so confidence cannot be assessed.",
  mixed: "Mixed — the independent axes point in opposite directions.",
};

function ConfidenceBlock({ comp }: { comp: CompositeSummary }) {
  const caption = comp.confidence_tier
    ? TIER_CAPTION[comp.confidence_tier]
    : undefined;
  return (
    <div className="w-full self-stretch rounded-sm border border-hairline bg-panel2/50 p-3">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[10px] font-semibold uppercase tracking-[0.16em] text-muted">
          Confidence
        </span>
        <TierChip tier={comp.confidence_tier} size="xs" />
      </div>

      <div className="num mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
        <span title="Lower confidence bound: composite minus dispersion + coverage penalty">
          LCB{" "}
          <span className="font-medium" style={{ color: compositeColor(comp.lcb) }}>
            {composite(comp.lcb)}
          </span>
        </span>
        <span title="Standard error across the independent axes">
          stderr{" "}
          <span className="text-paper">
            {comp.score_stderr_eff == null
              ? DASH
              : `±${num(comp.score_stderr_eff, { decimals: 2 })}`}
          </span>
        </span>
        {comp.factor_version && <span>{comp.factor_version}</span>}
      </div>

      <div className="mt-2.5 grid grid-cols-4 gap-2">
        {AXIS_ORDER.map(({ key, label }) => {
          const v = comp.axis_scores?.[key] ?? null;
          return (
            <div key={key}>
              <div className="text-[9px] font-semibold uppercase tracking-[0.12em] text-muted">
                {label}
              </div>
              <div
                className="num mt-0.5 text-sm"
                style={{
                  color: v == null ? "var(--color-muted)" : compositeColor(v),
                }}
              >
                {v == null ? DASH : v.toFixed(0)}
              </div>
            </div>
          );
        })}
      </div>

      {caption && <p className="mt-2 text-[11px] leading-snug text-muted">{caption}</p>}

      <p className="wisdom mt-2 max-w-[280px] text-[11px] leading-snug text-muted">
        The 10-persona council carries roughly 2–3 independent signals —
        confidence is measured across those axes, not by counting agreeing
        voices.
      </p>
    </div>
  );
}

function Stat({
  label,
  value,
  color,
}: {
  label: string;
  value: string;
  color?: string;
}) {
  return (
    <div>
      <div className="text-[10px] font-semibold uppercase tracking-[0.12em] text-muted">
        {label}
      </div>
      <div
        className="num mt-0.5 text-lg font-medium"
        style={{ color: color ?? "var(--color-paper)" }}
      >
        {value}
      </div>
    </div>
  );
}

function BackLink() {
  return (
    <Link
      href="/"
      className="inline-flex items-center gap-1.5 text-xs font-medium text-muted transition-colors hover:text-brass"
    >
      <span aria-hidden>←</span> Back to Discovery
    </Link>
  );
}

function StockSkeleton() {
  return (
    <div className="space-y-5">
      <Skeleton style={{ width: 140, height: 12 }} />
      <div className="rounded-md border border-hairline bg-panel p-5">
        <Skeleton style={{ width: 120, height: 12 }} />
        <Skeleton className="mt-3" style={{ width: 320, height: 28 }} />
        <Skeleton className="mt-2" style={{ width: 200, height: 12 }} />
        <div className="mt-5 flex gap-6">
          {Array.from({ length: 5 }).map((_, i) => (
            <Skeleton key={i} style={{ width: 56, height: 30 }} />
          ))}
        </div>
      </div>
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        {Array.from({ length: 4 }).map((_, i) => (
          <Skeleton key={i} style={{ height: 150 }} />
        ))}
      </div>
    </div>
  );
}
