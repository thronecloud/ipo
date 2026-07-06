"use client";

import Link from "next/link";
import { useCallback, useMemo, useState } from "react";
import { api, StockQuery } from "@/lib/api";
import { useAsync, useDebounced, useLocalStorage } from "@/lib/hooks";
import type { Meta, StockList, StockRow } from "@/lib/types";
import { DEFAULT_FILTERS, Filters } from "@/lib/filters";
import { PERSONA_SLUGS } from "@/lib/personas";
import { effectiveComposite } from "@/lib/compute";
import { composite, compositeColor, crore, num, pct, relTime, DASH } from "@/lib/format";
import FilterRail from "@/components/FilterRail";
import DataTable, { Column, SortState } from "@/components/DataTable";
import ConvictionStrip from "@/components/ConvictionStrip";
import RecChip from "@/components/RecChip";
import ScoreBar from "@/components/ScoreBar";
import TierChip from "@/components/TierChip";
import { ErrorState, EmptyState, TableSkeleton } from "@/components/States";

const PAGE_SIZE = 50;

export default function DiscoveryPage() {
  const [filters, setFilters] = useLocalStorage<Filters>(
    "wi.filters",
    DEFAULT_FILTERS,
  );
  const [selectedPersonas, setSelectedPersonas] = useLocalStorage<string[]>(
    "wi.personas",
    PERSONA_SLUGS,
  );
  const [watchlist, setWatchlist] = useLocalStorage<string[]>("wi.watchlist", []);
  // Default rank key is the LCB — composite minus a confidence penalty —
  // so conviction the engine can't back doesn't float to the top.
  const [sort, setSort] = useState<SortState>({
    key: "lcb",
    order: "desc",
  });
  const [page, setPage] = useState(1);

  const debouncedQ = useDebounced(filters.q, 350);

  const metaFetcher = useCallback((s: AbortSignal) => api.meta(s), []);
  const { data: meta } = useAsync<Meta>(metaFetcher, [], { refreshMs: 60_000 });

  const query: StockQuery = useMemo(
    () => ({
      universe: filters.universe || undefined,
      sector: filters.sector || undefined,
      cap: filters.cap || undefined,
      consensus: filters.consensus || undefined,
      q: debouncedQ.trim() || undefined,
      min_score: filters.minScore > 0 ? filters.minScore : undefined,
      max_score: filters.maxScore < 100 ? filters.maxScore : undefined,
      analyzed_only: filters.analyzedOnly || undefined,
      sort: sort.key,
      order: sort.order,
      page,
      page_size: PAGE_SIZE,
    }),
    [filters, debouncedQ, sort, page],
  );

  const stockFetcher = useCallback(
    (s: AbortSignal) => api.stocks(query, s),
    [query],
  );
  const { data, loading, error, refetch } = useAsync<StockList>(
    stockFetcher,
    [query],
    { refreshMs: 60_000 },
  );

  const patchFilters = useCallback(
    (patch: Partial<Filters>) => {
      setFilters((f) => ({ ...f, ...patch }));
      setPage(1);
    },
    [setFilters],
  );

  const resetFilters = useCallback(() => {
    setFilters(DEFAULT_FILTERS);
    setPage(1);
  }, [setFilters]);

  function toggleWatch(symbol: string) {
    setWatchlist((w) =>
      w.includes(symbol) ? w.filter((s) => s !== symbol) : [...w, symbol],
    );
  }

  const total = meta?.personas.length ?? 10;
  const watchSet = new Set(watchlist);

  // Client-side: watchlist filter + persona re-ranking of the loaded page.
  const rows = useMemo(() => {
    let items = data?.items ?? [];
    if (filters.watchlistOnly) {
      items = items.filter((r) => watchSet.has(r.symbol));
    }
    const subset = selectedPersonas.length < total;
    if (subset && sort.key === "composite_score") {
      items = [...items].sort((a, b) => {
        const ea = effectiveComposite(a, selectedPersonas, total).composite ?? -1;
        const eb = effectiveComposite(b, selectedPersonas, total).composite ?? -1;
        return sort.order === "desc" ? eb - ea : ea - eb;
      });
    }
    return items;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, filters.watchlistOnly, watchlist, selectedPersonas, sort, total]);

  function handleSort(key: string) {
    setSort((s) =>
      s.key === key
        ? { key, order: s.order === "desc" ? "asc" : "desc" }
        : { key, order: key === "symbol" ? "asc" : "desc" },
    );
    setPage(1);
  }

  const columns: Column<StockRow>[] = useMemo(() => {
    const startRank = (page - 1) * PAGE_SIZE;
    return [
      {
        key: "watch",
        header: "",
        width: 26,
        render: (r) => (
          <button
            onClick={(e) => {
              e.stopPropagation();
              toggleWatch(r.symbol);
            }}
            aria-label={
              watchSet.has(r.symbol)
                ? `Remove ${r.symbol} from watchlist`
                : `Add ${r.symbol} to watchlist`
            }
            className={`transition-colors ${watchSet.has(r.symbol) ? "text-brass" : "text-hairline hover:text-muted"}`}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill={watchSet.has(r.symbol) ? "currentColor" : "none"} stroke="currentColor" strokeWidth="1.5">
              <path d="m12 3 2.6 5.3 5.9.9-4.3 4.1 1 5.8L12 16.9 6.8 19.7l1-5.8L3.5 9.8l5.9-.9Z" strokeLinejoin="round" />
            </svg>
          </button>
        ),
      },
      {
        key: "rank",
        header: "#",
        align: "right",
        width: 34,
        render: (_r, i) => (
          <span className="num text-xs text-muted">{startRank + i + 1}</span>
        ),
      },
      {
        key: "strip",
        header: "Council",
        width: 118,
        render: (r) => <ConvictionStrip perPersona={r.per_persona} size="xs" />,
      },
      {
        key: "symbol",
        header: "Symbol",
        sortKey: "symbol",
        render: (r) => (
          <Link
            href={`/stock/${encodeURIComponent(r.symbol)}`}
            onClick={(e) => e.stopPropagation()}
            className="num font-medium text-paper hover:text-brass"
          >
            {r.symbol}
          </Link>
        ),
      },
      {
        key: "company",
        header: "Company",
        render: (r) => (
          <span className="serif block max-w-[220px] truncate text-[15px] text-paper/90">
            {r.company_name}
          </span>
        ),
      },
      {
        key: "lcb",
        header: "Conviction (LCB)",
        sortKey: "lcb",
        align: "left",
        width: 132,
        render: (r) => {
          if (r.lcb == null)
            return <span className="num text-xs text-muted">{DASH}</span>;
          return (
            <div className="flex items-center gap-1.5">
              <span
                className="num text-xs"
                style={{ color: compositeColor(r.lcb), minWidth: 30 }}
                title="Lower confidence bound: composite minus dispersion + coverage penalty"
              >
                {composite(r.lcb)}
              </span>
              {r.confidence_tier && <TierChip tier={r.confidence_tier} size="xs" />}
            </div>
          );
        },
      },
      {
        key: "composite",
        header: "Composite",
        sortKey: "composite_score",
        align: "left",
        width: 128,
        render: (r) => {
          const eff = effectiveComposite(r, selectedPersonas, total);
          if (eff.composite === null)
            return <span className="text-xs text-muted">not analyzed</span>;
          return (
            <div>
              <ScoreBar value={eff.composite} />
              {r.composite_updated_at && (
                <span
                  className="num mt-0.5 block text-[9px] leading-none text-muted"
                  title={`Last analyzed ${new Date(r.composite_updated_at).toLocaleString("en-IN")}`}
                >
                  {relTime(r.composite_updated_at)}
                </span>
              )}
            </div>
          );
        },
      },
      {
        key: "consensus",
        header: (
          <CallFilter
            value={filters.consensus}
            onChange={(v) => patchFilters({ consensus: v })}
          />
        ),
        width: 74,
        render: (r) => {
          const eff = effectiveComposite(r, selectedPersonas, total);
          return <RecChip rec={eff.consensus} size="xs" />;
        },
      },
      {
        key: "sector",
        header: "Sector",
        render: (r) => (
          <span className="block max-w-[150px] truncate text-xs text-muted">
            {r.sector ?? DASH}
          </span>
        ),
      },
      {
        key: "mcap",
        header: "MCap",
        sortKey: "market_cap_cr",
        align: "right",
        render: (r) => (
          <span className="num text-xs text-paper/90">{crore(r.market_cap_cr)}</span>
        ),
      },
      {
        key: "pe",
        header: "P/E",
        sortKey: "pe_ratio",
        align: "right",
        render: (r) => (
          <span className="num text-xs text-paper/90">
            {r.pe_ratio == null ? DASH : num(r.pe_ratio, { decimals: 1 })}
          </span>
        ),
      },
      {
        key: "growth",
        header: "Rev Δ",
        sortKey: "revenue_growth",
        align: "right",
        render: (r) => {
          if (r.revenue_growth == null)
            return <span className="num text-xs text-muted">{DASH}</span>;
          const positive = r.revenue_growth >= 0;
          return (
            <span
              className="num text-xs"
              style={{
                color: positive ? "var(--color-sage)" : "var(--color-terracotta)",
              }}
            >
              {pct(r.revenue_growth)}
            </span>
          );
        },
      },
    ];
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedPersonas, total, page, watchlist, filters.consensus, patchFilters]);

  const totalCount = data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(totalCount / PAGE_SIZE));
  const showing = rows.length;

  return (
    <div className="grid grid-cols-1 gap-5 lg:grid-cols-[260px_minmax(0,1fr)]">
      <div className="lg:sticky lg:top-16 lg:h-fit">
        <FilterRail
          meta={meta}
          filters={filters}
          onChange={patchFilters}
          onReset={resetFilters}
          selectedPersonas={selectedPersonas}
          onPersonasChange={(next) => {
            setSelectedPersonas(next);
            setPage(1);
          }}
          watchlistCount={watchlist.length}
        />
      </div>

      <div className="min-w-0">
        {/* header line */}
        <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
          <h1 className="serif text-2xl font-semibold text-paper">
            Discovery
          </h1>
          <p className="num text-xs text-muted">
            {loading && !data ? (
              "loading…"
            ) : (
              <>
                <span className="text-paper">{showing}</span> shown ·{" "}
                <span className="text-paper">
                  {totalCount.toLocaleString("en-IN")}
                </span>{" "}
                match filters
                {meta && (
                  <>
                    {" "}
                    · <span className="text-paper">{meta.analyzed.toLocaleString("en-IN")}</span>{" "}
                    of {meta.stocks_total.toLocaleString("en-IN")} analyzed
                  </>
                )}
              </>
            )}
          </p>
        </div>

        <div className="rounded-md border border-hairline bg-panel">
          {error ? (
            <ErrorState message={error} onRetry={refetch} />
          ) : loading && !data ? (
            <TableSkeleton rows={14} />
          ) : rows.length === 0 ? (
            <EmptyState
              title="No stocks match."
              hint="Loosen the filters, clear the watchlist toggle, or widen the composite range."
            />
          ) : (
            <DataTable<StockRow>
              columns={columns}
              rows={rows}
              rowKey={(r) => r.symbol}
              sort={sort}
              onSort={handleSort}
            />
          )}
        </div>

        {/* pagination */}
        {!error && totalCount > 0 && !filters.watchlistOnly && (
          <div className="mt-3 flex items-center justify-between">
            <span className="num text-xs text-muted">
              Page {page} / {totalPages}
            </span>
            <div className="flex items-center gap-1">
              <PageBtn
                disabled={page <= 1}
                onClick={() => setPage((p) => Math.max(1, p - 1))}
              >
                ← Prev
              </PageBtn>
              <PageBtn
                disabled={page >= totalPages}
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
              >
                Next →
              </PageBtn>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

const CALL_OPTIONS = [
  { value: "", label: "Call" },
  { value: "BUY", label: "Buy" },
  { value: "HOLD", label: "Hold" },
  { value: "AVOID", label: "Avoid" },
];

// Column-header filter for the Call column — drives the same consensus filter
// as the sidebar, so the two stay in sync.
function CallFilter({
  value,
  onChange,
}: {
  value: string;
  onChange: (v: string) => void;
}) {
  const active = value !== "";
  return (
    <span className="relative inline-flex items-center">
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        aria-label="Filter by call"
        className={`cursor-pointer appearance-none rounded-sm border bg-transparent py-0.5 pl-1 pr-4 text-[10px] font-semibold uppercase tracking-[0.1em] outline-none transition-colors ${
          active
            ? "border-brass/50 text-brass"
            : "border-transparent text-muted hover:text-paper"
        }`}
      >
        {CALL_OPTIONS.map((o) => (
          <option key={o.value} value={o.value} className="bg-panel text-paper">
            {o.label}
          </option>
        ))}
      </select>
      <span
        className={`pointer-events-none absolute right-1 text-[8px] ${active ? "text-brass" : "text-hairline"}`}
        aria-hidden
      >
        ▾
      </span>
    </span>
  );
}

function PageBtn({
  children,
  disabled,
  onClick,
}: {
  children: React.ReactNode;
  disabled?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      disabled={disabled}
      onClick={onClick}
      className="rounded-sm border border-hairline bg-panel px-3 py-1.5 text-xs font-medium text-paper transition-colors enabled:hover:border-brass enabled:hover:text-brass disabled:cursor-not-allowed disabled:opacity-40"
    >
      {children}
    </button>
  );
}
