"use client";

import { useEffect, useState } from "react";
import type { Meta } from "@/lib/types";
import { DEFAULT_FILTERS, Filters, activeFilterCount } from "@/lib/filters";
import { titleCase } from "@/lib/format";
import PersonaWeighting from "./PersonaWeighting";

const CAPS = [
  { value: "", label: "All caps" },
  { value: "large", label: "Large" },
  { value: "mid", label: "Mid" },
  { value: "small", label: "Small" },
];

const CONSENSUS = [
  { value: "", label: "Any" },
  { value: "BUY", label: "Buy" },
  { value: "HOLD", label: "Hold" },
  { value: "AVOID", label: "Avoid" },
];

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-[10px] font-semibold uppercase tracking-[0.14em] text-muted">
        {label}
      </span>
      {children}
    </label>
  );
}

const selectCls =
  "w-full rounded-sm border border-hairline bg-panel px-2 py-1.5 text-sm text-paper outline-none focus:border-brass";

export default function FilterRail({
  meta,
  filters,
  onChange,
  onReset,
  selectedPersonas,
  onPersonasChange,
  watchlistCount,
}: {
  meta: Meta | null;
  filters: Filters;
  onChange: (patch: Partial<Filters>) => void;
  onReset: () => void;
  selectedPersonas: string[];
  onPersonasChange: (next: string[]) => void;
  watchlistCount: number;
}) {
  const [localQ, setLocalQ] = useState(filters.q);
  useEffect(() => setLocalQ(filters.q), [filters.q]);

  const universes = meta ? Object.entries(meta.universes) : [];
  const active = activeFilterCount(filters);

  return (
    <aside className="flex flex-col gap-4">
      {/* search */}
      <div>
        <div className="relative">
          <input
            type="search"
            value={localQ}
            onChange={(e) => {
              setLocalQ(e.target.value);
              onChange({ q: e.target.value });
            }}
            placeholder="Search symbol or company…"
            aria-label="Search stocks"
            className="w-full rounded-sm border border-hairline bg-panel py-2 pl-8 pr-2 text-sm text-paper outline-none placeholder:text-muted/70 focus:border-brass"
          />
          <svg
            className="pointer-events-none absolute left-2.5 top-2.5 text-muted"
            width="15"
            height="15"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            aria-hidden
          >
            <circle cx="11" cy="11" r="7" />
            <path d="m21 21-4.3-4.3" strokeLinecap="round" />
          </svg>
        </div>
      </div>

      <div className="rounded-md border border-hairline bg-panel p-3.5">
        <div className="mb-3 flex items-center justify-between">
          <span className="text-[10px] font-semibold uppercase tracking-[0.16em] text-muted">
            Filters {active > 0 && <span className="text-brass">· {active}</span>}
          </span>
          {active > 0 && (
            <button
              onClick={onReset}
              className="text-[10px] font-medium text-brass hover:underline"
            >
              reset
            </button>
          )}
        </div>

        <div className="space-y-3">
          <Field label="Universe">
            <select
              className={selectCls}
              value={filters.universe}
              onChange={(e) => onChange({ universe: e.target.value })}
            >
              <option value="">All universes</option>
              {universes.map(([tag, count]) => (
                <option key={tag} value={tag}>
                  {titleCase(tag)} ({count})
                </option>
              ))}
            </select>
          </Field>

          <Field label="Sector">
            <select
              className={selectCls}
              value={filters.sector}
              onChange={(e) => onChange({ sector: e.target.value })}
            >
              <option value="">All sectors</option>
              {(meta?.sectors ?? []).map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </Field>

          <div className="grid grid-cols-2 gap-2">
            <Field label="Cap">
              <select
                className={selectCls}
                value={filters.cap}
                onChange={(e) => onChange({ cap: e.target.value })}
              >
                {CAPS.map((c) => (
                  <option key={c.value} value={c.value}>
                    {c.label}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Consensus">
              <select
                className={selectCls}
                value={filters.consensus}
                onChange={(e) => onChange({ consensus: e.target.value })}
              >
                {CONSENSUS.map((c) => (
                  <option key={c.value} value={c.value}>
                    {c.label}
                  </option>
                ))}
              </select>
            </Field>
          </div>

          {/* score range */}
          <div>
            <div className="mb-1 flex items-center justify-between">
              <span className="text-[10px] font-semibold uppercase tracking-[0.14em] text-muted">
                Composite range
              </span>
              <span className="num text-[11px] text-paper">
                {filters.minScore}–{filters.maxScore}
              </span>
            </div>
            <div className="flex items-center gap-2">
              <input
                type="range"
                min={0}
                max={100}
                value={filters.minScore}
                aria-label="Minimum composite score"
                onChange={(e) =>
                  onChange({
                    minScore: Math.min(Number(e.target.value), filters.maxScore),
                  })
                }
                className="h-1 w-full accent-brass"
              />
              <input
                type="range"
                min={0}
                max={100}
                value={filters.maxScore}
                aria-label="Maximum composite score"
                onChange={(e) =>
                  onChange({
                    maxScore: Math.max(Number(e.target.value), filters.minScore),
                  })
                }
                className="h-1 w-full accent-brass"
              />
            </div>
          </div>

          {/* toggles */}
          <div className="space-y-1.5 pt-1">
            <Toggle
              label="Analyzed only"
              checked={filters.analyzedOnly}
              onChange={(v) => onChange({ analyzedOnly: v })}
            />
            <Toggle
              label={`Watchlist only (${watchlistCount})`}
              checked={filters.watchlistOnly}
              onChange={(v) => onChange({ watchlistOnly: v })}
              disabled={watchlistCount === 0}
            />
          </div>
        </div>
      </div>

      <div className="rounded-md border border-hairline bg-panel p-3.5">
        <PersonaWeighting
          selected={selectedPersonas}
          onChange={onPersonasChange}
        />
      </div>
    </aside>
  );
}

function Toggle({
  label,
  checked,
  onChange,
  disabled,
}: {
  label: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={`flex w-full items-center justify-between rounded-sm px-1 py-1 text-left text-sm transition-colors ${
        disabled ? "cursor-not-allowed opacity-40" : "hover:text-paper"
      } ${checked ? "text-paper" : "text-muted"}`}
    >
      <span>{label}</span>
      <span
        className={`relative inline-block h-4 w-7 rounded-full transition-colors ${
          checked ? "bg-brass" : "bg-hairline"
        }`}
      >
        <span
          className="absolute top-0.5 h-3 w-3 rounded-full bg-ink transition-all"
          style={{ left: checked ? 14 : 2 }}
        />
      </span>
    </button>
  );
}

export { DEFAULT_FILTERS };
