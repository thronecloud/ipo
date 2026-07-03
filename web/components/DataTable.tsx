"use client";

import type { ReactNode } from "react";

export interface Column<T> {
  key: string;
  header: ReactNode;
  render: (row: T, index: number) => ReactNode;
  align?: "left" | "right" | "center";
  sortKey?: string; // if set, header is a sort control
  width?: number | string;
  className?: string;
  headerClassName?: string;
}

export interface SortState {
  key: string;
  order: "asc" | "desc";
}

function SortIcon({ active, order }: { active: boolean; order: "asc" | "desc" }) {
  return (
    <span
      className="ml-1 inline-block text-[9px] leading-none"
      style={{ color: active ? "var(--color-brass)" : "var(--color-hairline)" }}
      aria-hidden
    >
      {active ? (order === "asc" ? "▲" : "▼") : "▾"}
    </span>
  );
}

export default function DataTable<T>({
  columns,
  rows,
  rowKey,
  sort,
  onSort,
  onRowClick,
  dense = true,
}: {
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T, index: number) => string;
  sort?: SortState;
  onSort?: (key: string) => void;
  onRowClick?: (row: T) => void;
  dense?: boolean;
}) {
  const pad = dense ? "px-3 py-2" : "px-4 py-3";
  return (
    <div className="w-full overflow-x-auto">
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr className="border-b border-hairline">
            {columns.map((c) => {
              const alignCls =
                c.align === "right"
                  ? "text-right"
                  : c.align === "center"
                    ? "text-center"
                    : "text-left";
              const sortable = c.sortKey && onSort;
              const active = sort?.key === c.sortKey;
              return (
                <th
                  key={c.key}
                  scope="col"
                  style={{ width: c.width }}
                  className={`${pad} ${alignCls} text-[10px] font-semibold uppercase tracking-[0.1em] text-muted ${c.headerClassName ?? ""}`}
                >
                  {sortable ? (
                    <button
                      onClick={() => onSort!(c.sortKey!)}
                      className={`inline-flex items-center whitespace-nowrap transition-colors hover:text-paper ${active ? "text-brass" : ""} ${c.align === "right" ? "flex-row-reverse" : ""}`}
                    >
                      {c.header}
                      <SortIcon active={!!active} order={sort?.order ?? "desc"} />
                    </button>
                  ) : (
                    <span className="whitespace-nowrap">{c.header}</span>
                  )}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr
              key={rowKey(row, i)}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
              className={`border-b border-hairline/60 transition-colors last:border-0 hover:bg-panel2/60 ${onRowClick ? "cursor-pointer" : ""}`}
            >
              {columns.map((c) => {
                const alignCls =
                  c.align === "right"
                    ? "text-right"
                    : c.align === "center"
                      ? "text-center"
                      : "text-left";
                return (
                  <td
                    key={c.key}
                    className={`${pad} ${alignCls} align-middle ${c.className ?? ""}`}
                  >
                    {c.render(row, i)}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
