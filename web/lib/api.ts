// Typed client for the WisdomInvest FastAPI read-layer.
//
// In the browser we always call same-origin "/api/*" — next.config rewrites
// proxy that to the backend, so there is no CORS surface. On the server (RSC),
// or if NEXT_PUBLIC_API_BASE is set, we hit the absolute base directly.

import type {
  AdminOverview,
  BacktestStudy,
  CandleRange,
  CandleSeries,
  CoverageRow,
  DataQualityOverview,
  JobRun,
  PersonaStudy,
  JobRunResponse,
  JobType,
  Meta,
  PersonaDistribution,
  ReconciliationOverview,
  SchedulerOverview,
  StockDetail,
  StockList,
  Usage,
  VintageStudy,
} from "./types";

const ABSOLUTE_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

function apiRoot(): string {
  // On the client, use the same-origin proxy so dev + prod behave identically.
  if (typeof window !== "undefined") return "";
  return ABSOLUTE_BASE;
}

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  const url = `${apiRoot()}${path}`;
  let res: Response;
  try {
    res = await fetch(url, {
      signal,
      headers: { Accept: "application/json" },
      cache: "no-store",
    });
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") throw e;
    throw new ApiError(
      "Could not reach the WisdomInvest engine. Is the API running on :8000?",
      0,
    );
  }
  if (!res.ok) {
    let detail = `Request failed (${res.status})`;
    try {
      const body = await res.json();
      if (body?.detail) detail = String(body.detail);
    } catch {
      /* ignore body parse errors */
    }
    throw new ApiError(detail, res.status);
  }
  return (await res.json()) as T;
}

// ── Query building ────────────────────────────────────────────────

export interface StockQuery {
  universe?: string;
  sector?: string;
  cap?: string;
  consensus?: string;
  q?: string;
  min_score?: number;
  max_score?: number;
  analyzed_only?: boolean;
  sort?: string;
  order?: "asc" | "desc";
  page?: number;
  page_size?: number;
}

function toQS(params: Record<string, unknown>): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    if (typeof v === "boolean") {
      if (v) sp.set(k, "true");
      continue;
    }
    sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
}

// ── Consumer endpoints ────────────────────────────────────────────

export const api = {
  meta: (signal?: AbortSignal) => get<Meta>("/api/meta", signal),

  stocks: (query: StockQuery = {}, signal?: AbortSignal) =>
    get<StockList>(`/api/stocks${toQS(query as Record<string, unknown>)}`, signal),

  stock: (symbol: string, signal?: AbortSignal) =>
    get<StockDetail>(`/api/stocks/${encodeURIComponent(symbol)}`, signal),

  candles: (symbol: string, range: CandleRange = "1y", signal?: AbortSignal) =>
    get<CandleSeries>(
      `/api/stocks/${encodeURIComponent(symbol)}/candles?range=${range}`,
      signal,
    ),

  backtest: (signal?: AbortSignal) =>
    get<BacktestStudy>("/api/backtest", signal),

  backtestPersonas: (personas?: string[], signal?: AbortSignal) =>
    get<PersonaStudy>(
      `/api/backtest/personas${toQS({ personas: personas?.length ? personas.join(",") : undefined })}`,
      signal,
    ),

  backtestVintages: (
    opts: { step?: number; hold?: number } = {},
    signal?: AbortSignal,
  ) =>
    get<VintageStudy>(
      `/api/backtest/vintages${toQS(opts as Record<string, unknown>)}`,
      signal,
    ),

  // ── Admin endpoints ───────────────────────────────────────────
  adminOverview: (signal?: AbortSignal) =>
    get<AdminOverview>("/api/admin/overview", signal),

  adminJobs: (opts: { limit?: number; type?: string } = {}, signal?: AbortSignal) =>
    get<JobRun[]>(`/api/admin/jobs${toQS(opts)}`, signal),

  adminCoverage: (signal?: AbortSignal) =>
    get<CoverageRow[]>("/api/admin/coverage", signal),

  adminPersonas: (signal?: AbortSignal) =>
    get<PersonaDistribution[]>("/api/admin/personas/distribution", signal),

  adminUsage: (signal?: AbortSignal) => get<Usage>("/api/admin/usage", signal),

  adminScheduler: (job?: string, signal?: AbortSignal) =>
    get<SchedulerOverview>(`/api/admin/scheduler${toQS({ job })}`, signal),

  adminDataQuality: (signal?: AbortSignal) =>
    get<DataQualityOverview>("/api/admin/data-quality", signal),

  adminReconciliation: (signal?: AbortSignal) =>
    get<ReconciliationOverview>("/api/admin/reconciliation", signal),

  runJob: async (
    job: JobType,
    args: Record<string, unknown> = {},
    adminToken?: string,
  ): Promise<JobRunResponse> => {
    const url = `${apiRoot()}/api/admin/jobs/run`;
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      Accept: "application/json",
    };
    if (adminToken) headers["X-Admin-Token"] = adminToken;
    let res: Response;
    try {
      res = await fetch(url, {
        method: "POST",
        headers,
        body: JSON.stringify({ job, args }),
      });
    } catch {
      throw new ApiError("Could not reach the engine to launch the job.", 0);
    }
    if (!res.ok) {
      let detail = `Job launch failed (${res.status})`;
      try {
        const body = await res.json();
        if (body?.detail) detail = String(body.detail);
      } catch {
        /* ignore */
      }
      throw new ApiError(detail, res.status);
    }
    return (await res.json()) as JobRunResponse;
  },
};
