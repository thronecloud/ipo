"use client";

import { useMemo } from "react";
import type { AttributionBlock, CI, SectorIc, Verdict } from "@/lib/types";
import { icInsufficient } from "@/lib/types";
import { DASH } from "@/lib/format";

// "Attribution — skill or tilt?" Everything is measured against one smallcap
// benchmark, so a size or sector bet can beat it without the score picking a
// single stock well. This panel strips the size and sector tilt out of the
// excess returns and shows whether the composite's edge survives.

const VERDICT_LABEL: Record<Verdict, string> = {
  positive: "positive",
  negative: "negative",
  "indistinguishable from zero": "≈ zero",
};

function verdictColor(v: Verdict): string {
  if (v === "positive") return "var(--color-sage)";
  if (v === "negative") return "var(--color-terracotta)";
  return "var(--color-muted)";
}

function icColor(v: number | null | undefined): string {
  if (v == null) return "var(--color-muted)";
  return v >= 0 ? "var(--color-sage)" : "var(--color-terracotta)";
}

function fmtIc(v: number | null | undefined): string {
  if (v == null) return DASH;
  return `${v > 0 ? "+" : ""}${v.toFixed(3)}`;
}

function ciRange(ci: CI | null): string {
  if (!ci) return "too few names to bootstrap a CI";
  const f = (x: number) => `${x > 0 ? "+" : ""}${x.toFixed(3)}`;
  return `95% CI ${f(ci.ci_low)} to ${f(ci.ci_high)} · ${ci.verdict}`;
}

const HORIZON_LABEL: Record<number, string> = {
  5: "1 week",
  21: "1 month",
  63: "3 months",
  126: "6 months",
};
function horizonLabel(h: number): string {
  return HORIZON_LABEL[h] ?? `${h} trading days`;
}

// One IC card — a point estimate, its CI, and a verdict chip.
function IcCard({ label, hint, ci }: { label: string; hint: string; ci: CI | null }) {
  const point = ci?.point ?? null;
  return (
    <div className="flex-1 rounded-sm border border-hairline bg-panel2/40 p-3">
      <p className="text-[10px] uppercase tracking-wide text-muted" title={hint}>
        {label}
      </p>
      <p className="num mt-1 text-2xl" style={{ color: icColor(point) }}>
        {fmtIc(point)}
      </p>
      {ci ? (
        <p className="mt-1 text-[10px] text-muted">
          <span style={{ color: verdictColor(ci.verdict) }}>
            {VERDICT_LABEL[ci.verdict]}
          </span>{" "}
          · 95% CI {ci.ci_low.toFixed(2)} to {ci.ci_high.toFixed(2)}
        </p>
      ) : (
        <p className="mt-1 text-[10px] text-muted">no correlation to report</p>
      )}
    </div>
  );
}

// Ranked diverging bars for per-sector IC — the PersonaRanking pattern: a centre
// line, positive right / negative left, muted when the CI straddles zero.
function SectorBars({ rows }: { rows: [string, SectorIc][] }) {
  const parsed = useMemo(
    () =>
      rows.map(([sector, ic]) => {
        if (icInsufficient(ic)) {
          return { sector, v: null as number | null, ci: null as CI | null, n: ic.n, insufficient: true };
        }
        return { sector, v: ic.point, ci: ic, n: ic.n, insufficient: false };
      }),
    [rows],
  );

  const ranked = useMemo(
    () =>
      [...parsed].sort((a, b) => {
        if (a.v == null && b.v == null) return b.n - a.n;
        if (a.v == null) return 1;
        if (b.v == null) return -1;
        return b.v - a.v;
      }),
    [parsed],
  );

  const maxAbs = useMemo(() => {
    const vs = ranked.map((r) => r.v).filter((v): v is number => v != null);
    return Math.max(0.2, ...vs.map((v) => Math.abs(v)));
  }, [ranked]);

  if (!ranked.length) {
    return <p className="text-xs text-muted">No sectors to rank.</p>;
  }

  return (
    <div className="space-y-1">
      {ranked.map(({ sector, v, ci, n, insufficient }) => {
        const pos = v != null && v >= 0;
        const frac = v == null ? 0 : Math.min(1, Math.abs(v) / maxAbs);
        const noise = ci != null && ci.verdict === "indistinguishable from zero";
        const title = insufficient
          ? `${sector}: only ${n} measured names — too few to correlate`
          : `${sector}: IC ${fmtIc(v)} over ${n} names · ${ciRange(ci)}`;
        return (
          <div
            key={sector}
            title={title}
            className="flex items-center gap-2 rounded-sm px-1.5 py-1"
          >
            <span className="w-28 shrink-0 truncate text-[11px] text-muted">
              {sector}
            </span>
            <span className="relative flex h-3.5 flex-1 items-center">
              <span className="absolute left-1/2 h-full w-px bg-hairline" />
              {v != null && (
                <span
                  className="absolute h-2 rounded-[1px]"
                  style={{
                    width: `${(frac * 50).toFixed(1)}%`,
                    [pos ? "left" : "right"]: "50%",
                    backgroundColor: pos
                      ? "var(--color-sage)"
                      : "var(--color-terracotta)",
                  }}
                />
              )}
            </span>
            <span
              className="num w-14 shrink-0 text-right text-[11px]"
              style={{
                color:
                  v == null || noise ? "var(--color-muted)" : icColor(v),
              }}
            >
              {insufficient ? `n=${n}` : fmtIc(v)}
            </span>
          </div>
        );
      })}
    </div>
  );
}

export default function AttributionPanel({ attr }: { attr: AttributionBlock }) {
  const sectorRows = useMemo(
    () => Object.entries(attr.per_sector_ic),
    [attr.per_sector_ic],
  );

  if (attr.insufficient) {
    return (
      <div className="p-4">
        <p className="text-sm text-muted">
          Not enough of the cohort carries a score, a market cap and a{" "}
          {horizonLabel(attr.horizon)} return to separate skill from tilt.
          {attr.reason ? ` (${attr.reason})` : ""}
        </p>
      </div>
    );
  }

  const raw = attr.raw_ic?.point ?? null;
  const resid = attr.residual_ic?.point ?? null;
  // Plain-language read of what survived the strip.
  const survives =
    attr.residual_ic != null && attr.residual_ic.verdict === "positive";
  const collapsed =
    raw != null && resid != null && raw > 0.05 && Math.abs(resid) < raw * 0.4;
  const verdict = survives
    ? "The edge survives. After removing size and sector effects, a higher score still lines up with a higher return — that reads as stock-picking, not a tilt."
    : collapsed
      ? "The edge largely collapses. Most of the raw correlation was a size or sector bet: once those are stripped out, little stock-picking remains."
      : "Inconclusive. With the tilt removed, the score's link to returns is too noisy to call either way.";

  return (
    <div className="space-y-5 p-4">
      <p className="text-xs text-muted">
        Everything here is measured against one smallcap benchmark, so a size or
        sector bet can beat it without the score picking a single stock well.
        After removing size and sector effects, does the score still pick winners?
        Measured at {horizonLabel(attr.horizon)} over {attr.n} names.
      </p>

      <div className="flex flex-col gap-3 sm:flex-row">
        <IcCard
          label="Raw IC"
          hint="Spearman rank correlation of the composite against the excess return, before any adjustment."
          ci={attr.raw_ic}
        />
        <IcCard
          label="Tilt-stripped IC"
          hint="The same correlation against the residual excess return — size and sector regressed out."
          ci={attr.residual_ic}
        />
      </div>

      <p
        className="rounded-sm border-l-2 pl-3 text-sm"
        style={{
          borderColor: survives
            ? "var(--color-sage)"
            : collapsed
              ? "var(--color-terracotta)"
              : "var(--color-brass)",
          color: "var(--color-paper)",
        }}
      >
        {verdict}
      </p>

      <div>
        <p className="mb-2 text-[10px] uppercase tracking-wide text-muted">
          Does the score work inside a single sector?
        </p>
        <p className="mb-3 text-xs text-muted">
          Composite-vs-return rank correlation within each sector that has enough
          measured names — a within-sector edge is skill the benchmark tilt can't
          explain.
        </p>
        <SectorBars rows={sectorRows} />
      </div>

      <details className="text-xs text-muted">
        <summary className="cursor-pointer select-none text-[11px] uppercase tracking-wide">
          Regression detail — R², loadings
        </summary>
        <div className="mt-3 space-y-3">
          <div className="flex flex-wrap gap-x-6 gap-y-1">
            <span>
              R²{" "}
              <span className="num text-paper">
                {attr.r2 == null ? DASH : attr.r2.toFixed(3)}
              </span>
            </span>
            <span>
              Residual alpha{" "}
              <span className="num text-paper">
                {attr.alpha == null ? DASH : attr.alpha.toFixed(2)}
              </span>
            </span>
            <span title="Excess return per unit of log market cap.">
              Size loading{" "}
              <span className="num text-paper">
                {attr.size_loading == null ? DASH : attr.size_loading.toFixed(2)}
              </span>
            </span>
          </div>

          {attr.dummies_dropped && (
            <p style={{ color: "var(--color-brass)" }}>
              Sector dummies were dropped — the design was rank-deficient even
              after pooling, so only size and the intercept are fitted.
            </p>
          )}
          {attr.size_dropped && (
            <p style={{ color: "var(--color-brass)" }}>
              The size term was dropped — log market cap did not vary across the
              cohort.
            </p>
          )}

          {Object.keys(attr.sector_loadings).length > 0 && (
            <div>
              <p className="mb-1">
                Sector loadings (excess return vs the{" "}
                <span className="text-paper">
                  {attr.reference_sector ?? "reference"}
                </span>{" "}
                baseline):
              </p>
              <ul className="grid grid-cols-2 gap-x-6 gap-y-0.5 sm:grid-cols-3">
                {attr.modeled_sectors.map((s) => (
                  <li key={s} className="flex justify-between gap-2">
                    <span className="truncate">{s}</span>
                    <span
                      className="num"
                      style={{ color: icColor(attr.sector_loadings[s]) }}
                    >
                      {attr.sector_loadings[s] > 0 ? "+" : ""}
                      {attr.sector_loadings[s].toFixed(2)}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {attr.pooled_into_other.length > 0 && (
            <p>
              Pooled into “other”: {attr.pooled_into_other.join(", ")}.
            </p>
          )}
        </div>
      </details>
    </div>
  );
}
