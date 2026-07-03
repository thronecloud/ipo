"use client";

import { useState } from "react";
import type { Fundamentals as FundamentalsData } from "@/lib/types";
import { MiniBars } from "./Bars";
import { num } from "@/lib/format";

type Series = Record<string, number | null>;

function toNum(v: unknown): number | null {
  if (v === null || v === undefined) return null;
  if (typeof v === "number") return Number.isFinite(v) ? v : null;
  const cleaned = String(v).replace(/[,%₹\s]/g, "");
  const n = parseFloat(cleaned);
  return Number.isNaN(n) ? null : n;
}

function pickMetric(
  annual: Record<string, Series>,
  candidates: string[],
): [string, Series] | null {
  const keys = Object.keys(annual);
  for (const c of candidates) {
    const hit = keys.find((k) => k.toLowerCase().includes(c.toLowerCase()));
    if (hit) return [hit, annual[hit]];
  }
  return null;
}

function SmallMultiple({
  title,
  series,
  color,
  suffix = "",
}: {
  title: string;
  series: Series;
  color: string;
  suffix?: string;
}) {
  const periods = Object.keys(series);
  const values = periods.map((p) => toNum(series[p]) ?? 0);
  const nonNull = periods
    .map((p) => toNum(series[p]))
    .filter((v): v is number => v !== null);
  const latest = nonNull.at(-1);
  const prev = nonNull.at(-2);
  const delta =
    latest !== undefined && prev !== undefined && prev !== 0
      ? ((latest - prev) / Math.abs(prev)) * 100
      : null;

  return (
    <div className="rounded-md border border-hairline bg-panel p-3">
      <div className="flex items-baseline justify-between">
        <span className="text-[10px] font-semibold uppercase tracking-[0.12em] text-muted">
          {title}
        </span>
        {delta !== null && (
          <span
            className="num text-[10px]"
            style={{
              color: delta >= 0 ? "var(--color-sage)" : "var(--color-terracotta)",
            }}
          >
            {delta >= 0 ? "+" : ""}
            {delta.toFixed(0)}%
          </span>
        )}
      </div>
      <div className="num mt-1 text-lg font-semibold text-paper">
        {latest !== undefined ? num(latest, { decimals: 1, suffix }) : "—"}
      </div>
      <div className="mt-2">
        <MiniBars values={values} color={color} height={30} labels={periods} />
      </div>
      <div className="num mt-1 flex justify-between text-[9px] text-muted">
        <span>{periods[0]}</span>
        <span>{periods.at(-1)}</span>
      </div>
    </div>
  );
}

function TrendTable({ data }: { data: Record<string, Series> }) {
  const metrics = Object.keys(data);
  if (metrics.length === 0)
    return <p className="px-4 py-6 text-sm text-muted">No data available.</p>;
  const periods = Object.keys(data[metrics[0]] ?? {});
  return (
    <div className="w-full overflow-x-auto">
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr className="border-b border-hairline">
            <th className="px-3 py-2 text-left text-[10px] font-semibold uppercase tracking-wide text-muted">
              Metric
            </th>
            {periods.map((p) => (
              <th
                key={p}
                className="num px-3 py-2 text-right text-[10px] font-medium text-muted"
              >
                {p}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {metrics.map((m) => (
            <tr key={m} className="border-b border-hairline/50 last:border-0">
              <td className="px-3 py-1.5 text-left text-xs text-paper/90">{m}</td>
              {periods.map((p) => (
                <td
                  key={p}
                  className="num px-3 py-1.5 text-right text-xs text-paper/85"
                >
                  {toNum(data[m]?.[p]) === null
                    ? "—"
                    : num(toNum(data[m]?.[p]), { decimals: 1 })}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function Fundamentals({ data }: { data: FundamentalsData }) {
  const [tab, setTab] = useState<"annual" | "quarterly">("annual");
  const annual = data.annual ?? {};
  const quarterly = data.quarterly ?? {};
  const ratios = data.ratios ?? {};
  const shareholding = data.shareholding ?? {};

  const sales = pickMetric(annual, ["sales", "revenue", "total income"]);
  const profit = pickMetric(annual, ["net profit", "profit after", "pat"]);
  const opm = pickMetric(annual, ["opm", "operating margin", "ebitda margin"]);
  const eps = pickMetric(annual, ["eps"]);
  const smalls = [
    sales && { key: "Sales", s: sales[1], color: "var(--color-brass)", suffix: "" },
    profit && {
      key: "Net Profit",
      s: profit[1],
      color: "var(--color-sage)",
      suffix: "",
    },
    opm && { key: "OPM %", s: opm[1], color: "var(--color-brass)", suffix: "%" },
    eps && { key: "EPS", s: eps[1], color: "var(--color-paper)", suffix: "" },
  ].filter(Boolean) as {
    key: string;
    s: Series;
    color: string;
    suffix: string;
  }[];

  const ratioEntries = Object.entries(ratios).filter(
    ([, v]) => v !== null && v !== undefined && v !== "",
  );

  const hasAnnual = Object.keys(annual).length > 0;
  const hasShareholding = Object.keys(shareholding).length > 0;

  if (!hasAnnual && !hasShareholding && ratioEntries.length === 0) {
    return (
      <p className="px-4 py-8 text-center text-sm text-muted">
        Fundamentals are not available for this stock yet.
      </p>
    );
  }

  return (
    <div className="space-y-5">
      {/* small multiples */}
      {smalls.length > 0 && (
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          {smalls.map((s) => (
            <SmallMultiple
              key={s.key}
              title={s.key}
              series={s.s}
              color={s.color}
              suffix={s.suffix}
            />
          ))}
        </div>
      )}

      {/* ratios */}
      {ratioEntries.length > 0 && (
        <div>
          <h4 className="mb-2 text-[10px] font-semibold uppercase tracking-[0.14em] text-muted">
            Key ratios
          </h4>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">
            {ratioEntries.map(([k, v]) => (
              <div
                key={k}
                className="rounded-sm border border-hairline bg-panel px-3 py-2"
              >
                <div className="text-[10px] uppercase tracking-wide text-muted">
                  {k}
                </div>
                <div className="num mt-0.5 text-sm font-medium text-paper">
                  {typeof v === "number" ? num(v, { decimals: 2 }) : String(v)}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* annual / quarterly table */}
      {(hasAnnual || Object.keys(quarterly).length > 0) && (
        <div className="rounded-md border border-hairline bg-panel">
          <div className="flex items-center gap-1 border-b border-hairline px-3 py-2">
            {(["annual", "quarterly"] as const).map((t) => (
              <button
                key={t}
                onClick={() => setTab(t)}
                aria-pressed={tab === t}
                className={`rounded-sm px-2.5 py-1 text-xs font-medium capitalize transition-colors ${
                  tab === t ? "bg-panel2 text-brass" : "text-muted hover:text-paper"
                }`}
              >
                {t}
              </button>
            ))}
          </div>
          <TrendTable data={tab === "annual" ? annual : quarterly} />
        </div>
      )}

      {/* shareholding */}
      {hasShareholding && (
        <div>
          <h4 className="mb-2 text-[10px] font-semibold uppercase tracking-[0.14em] text-muted">
            Shareholding trend (%)
          </h4>
          <div className="rounded-md border border-hairline bg-panel">
            <TrendTable data={shareholding} />
          </div>
        </div>
      )}

      {data.about && (
        <div>
          <h4 className="mb-2 text-[10px] font-semibold uppercase tracking-[0.14em] text-muted">
            About
          </h4>
          <p className="text-sm leading-relaxed text-paper/85">{data.about}</p>
        </div>
      )}
    </div>
  );
}
