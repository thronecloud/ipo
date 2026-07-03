"use client";

import Link from "next/link";
import { useCallback } from "react";
import { api } from "@/lib/api";
import { useAsync } from "@/lib/hooks";
import type { Meta } from "@/lib/types";

export default function TopBar() {
  const fetcher = useCallback((s: AbortSignal) => api.meta(s), []);
  const { data } = useAsync<Meta>(fetcher, []);

  return (
    <header className="sticky top-0 z-40 border-b border-hairline bg-ink/90 backdrop-blur supports-[backdrop-filter]:bg-ink/80">
      <div className="mx-auto flex max-w-[1500px] items-center justify-between gap-4 px-4 py-2.5 sm:px-6">
        <div className="flex items-baseline gap-3">
          <Link
            href="/"
            className="serif text-xl font-semibold tracking-tight text-paper"
          >
            Wisdom<span className="text-brass">Invest</span>
          </Link>
          <span className="hidden text-[11px] uppercase tracking-[0.18em] text-muted sm:inline">
            The Terminal &amp; The Council
          </span>
        </div>

        <div className="flex items-center gap-4">
          <span className="num text-xs text-muted">
            {data ? (
              <>
                <span className="text-paper">{data.analyzed.toLocaleString("en-IN")}</span>{" "}
                of{" "}
                <span className="text-paper">
                  {data.stocks_total.toLocaleString("en-IN")}
                </span>{" "}
                analyzed
              </>
            ) : (
              <span className="text-muted">loading coverage…</span>
            )}
          </span>
          <Link
            href="/admin"
            className="rounded-sm border border-hairline px-2.5 py-1 text-xs font-medium text-muted transition-colors hover:border-brass hover:text-brass"
          >
            Engine Room
          </Link>
        </div>
      </div>
    </header>
  );
}
