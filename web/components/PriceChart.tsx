"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/lib/api";
import type { Candle, CandleRange } from "@/lib/types";
import { Panel, PanelHeader } from "./Panel";

// Token hexes (lightweight-charts can't read CSS vars) — kept in sync with globals.css.
const C = {
  panel: "#161a1f",
  hairline: "#262c34",
  paper: "#ede8dc",
  muted: "#8a93a0",
  brass: "#c8a24b",
  sage: "#4fb286",
  terracotta: "#d9614c",
} as const;

const RANGES: { key: CandleRange; label: string }[] = [
  { key: "1m", label: "1M" },
  { key: "6m", label: "6M" },
  { key: "1y", label: "1Y" },
  { key: "max", label: "MAX" },
];

type Mode = "candles" | "line";

export default function PriceChart({ symbol }: { symbol: string }) {
  const [range, setRange] = useState<CandleRange>("1y");
  const [mode, setMode] = useState<Mode>("candles");
  const [candles, setCandles] = useState<Candle[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const containerRef = useRef<HTMLDivElement>(null);

  // Fetch whenever symbol or range changes.
  useEffect(() => {
    const ctrl = new AbortController();
    setLoading(true);
    setError(null);
    api
      .candles(symbol, range, ctrl.signal)
      .then((res) => setCandles(res.candles))
      .catch((e: unknown) => {
        if (e instanceof DOMException && e.name === "AbortError") return;
        setError(e instanceof Error ? e.message : "Could not load price history.");
        setCandles(null);
      })
      .finally(() => setLoading(false));
    return () => ctrl.abort();
  }, [symbol, range]);

  // (Re)build the chart when data or mode changes. Dynamic import keeps
  // lightweight-charts out of the SSR path and code-splits it to this route.
  useEffect(() => {
    const el = containerRef.current;
    if (!el || !candles || candles.length === 0) return;

    let disposed = false;
    let chart: { remove: () => void; applyOptions: (o: object) => void } | null = null;
    let ro: ResizeObserver | null = null;

    (async () => {
      const LWC = await import("lightweight-charts");
      if (disposed || !containerRef.current) return;

      const c = LWC.createChart(el, {
        width: el.clientWidth,
        height: 380,
        autoSize: false,
        layout: {
          background: { type: LWC.ColorType.Solid, color: "transparent" },
          textColor: C.muted,
          fontFamily:
            "var(--font-plex-mono), ui-monospace, SFMono-Regular, monospace",
          fontSize: 11,
        },
        grid: {
          horzLines: { color: "rgba(38,44,52,0.55)" },
          vertLines: { visible: false },
        },
        crosshair: {
          mode: LWC.CrosshairMode.Normal,
          vertLine: {
            color: C.brass,
            width: 1,
            style: LWC.LineStyle.Dashed,
            labelBackgroundColor: C.brass,
          },
          horzLine: { color: C.brass, labelBackgroundColor: C.brass },
        },
        rightPriceScale: { borderColor: C.hairline },
        timeScale: { borderColor: C.hairline, rightOffset: 4 },
        handleScale: { axisPressedMouseMove: false },
      });
      chart = c;

      const bars = candles.filter((b) => b.close !== null);

      if (mode === "candles") {
        const s = c.addSeries(LWC.CandlestickSeries, {
          upColor: C.sage,
          downColor: C.terracotta,
          borderUpColor: C.sage,
          borderDownColor: C.terracotta,
          wickUpColor: C.sage,
          wickDownColor: C.terracotta,
        });
        s.setData(
          bars.map((b) => ({
            time: b.time,
            open: b.open ?? b.close!,
            high: b.high ?? b.close!,
            low: b.low ?? b.close!,
            close: b.close!,
          })),
        );
      } else {
        const s = c.addSeries(LWC.LineSeries, {
          color: C.brass,
          lineWidth: 2,
          priceLineVisible: false,
          crosshairMarkerBorderColor: C.brass,
          crosshairMarkerBackgroundColor: C.panel,
        });
        s.setData(bars.map((b) => ({ time: b.time, value: b.close! })));
      }

      // Volume histogram pinned to the bottom 22% via an overlay price scale.
      const vol = c.addSeries(LWC.HistogramSeries, {
        priceScaleId: "vol",
        priceFormat: { type: "volume" },
        lastValueVisible: false,
        priceLineVisible: false,
      });
      vol.setData(
        candles
          .filter((b) => b.volume !== null)
          .map((b) => ({
            time: b.time,
            value: b.volume!,
            color:
              (b.close ?? 0) >= (b.open ?? 0)
                ? "rgba(79,178,134,0.35)"
                : "rgba(217,97,76,0.35)",
          })),
      );
      c.priceScale("vol").applyOptions({
        scaleMargins: { top: 0.78, bottom: 0 },
        borderVisible: false,
      });

      c.timeScale().fitContent();

      ro = new ResizeObserver(() => {
        if (containerRef.current)
          c.applyOptions({ width: containerRef.current.clientWidth });
      });
      ro.observe(el);
    })();

    return () => {
      disposed = true;
      ro?.disconnect();
      chart?.remove();
    };
  }, [candles, mode]);

  const change = useMemo(() => {
    if (!candles || candles.length < 2) return null;
    const first = candles.find((c) => c.close !== null)?.close;
    const last = [...candles].reverse().find((c) => c.close !== null)?.close;
    if (first == null || last == null || first === 0) return null;
    return { abs: last - first, pct: ((last - first) / first) * 100 };
  }, [candles]);

  const empty = !loading && !error && candles !== null && candles.length === 0;

  return (
    <Panel>
      <PanelHeader
        title="Price"
        editorial
        right={
          <div className="flex items-center gap-3">
            {change && (
              <span
                className="num text-xs"
                style={{
                  color: change.abs >= 0 ? C.sage : C.terracotta,
                }}
              >
                {change.abs >= 0 ? "+" : ""}
                {change.pct.toFixed(1)}%{" "}
                <span className="text-muted">
                  {RANGES.find((r) => r.key === range)?.label}
                </span>
              </span>
            )}
            <Toggle
              options={[
                { key: "candles", label: "Candles" },
                { key: "line", label: "Line" },
              ]}
              value={mode}
              onChange={(v) => setMode(v as Mode)}
            />
            <Toggle
              options={RANGES.map((r) => ({ key: r.key, label: r.label }))}
              value={range}
              onChange={(v) => setRange(v as CandleRange)}
            />
          </div>
        }
      />
      <div className="p-4">
        {error ? (
          <p className="py-16 text-center text-sm text-terracotta">{error}</p>
        ) : empty ? (
          <p className="py-16 text-center text-sm text-muted">
            No price history yet — it accrues after the next data refresh.
          </p>
        ) : (
          <div className="relative">
            <div ref={containerRef} className="h-[380px] w-full" />
            {loading && (
              <div className="absolute inset-0 flex items-center justify-center">
                <span className="num text-xs text-muted">loading prices…</span>
              </div>
            )}
          </div>
        )}
      </div>
    </Panel>
  );
}

function Toggle({
  options,
  value,
  onChange,
}: {
  options: { key: string; label: string }[];
  value: string;
  onChange: (key: string) => void;
}) {
  return (
    <div className="inline-flex overflow-hidden rounded-sm border border-hairline">
      {options.map((o) => {
        const active = o.key === value;
        return (
          <button
            key={o.key}
            onClick={() => onChange(o.key)}
            aria-pressed={active}
            className={`px-2 py-1 text-[11px] font-medium transition-colors ${
              active
                ? "bg-brass/15 text-brass"
                : "text-muted hover:text-paper"
            }`}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}
