"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const LINKS = [
  { href: "/admin", label: "Overview" },
  { href: "/admin/data-quality", label: "Data Quality" },
  { href: "/admin/jobs", label: "Jobs" },
  { href: "/admin/coverage", label: "Coverage" },
  { href: "/admin/personas", label: "Personas" },
  { href: "/admin/backtest", label: "Backtest" },
];

export default function AdminNav() {
  const pathname = usePathname();
  return (
    <header className="sticky top-0 z-40 border-b border-hairline bg-ink/90 backdrop-blur">
      <div className="mx-auto flex max-w-[1500px] flex-wrap items-center justify-between gap-x-6 gap-y-2 px-4 py-2.5 sm:px-6">
        <div className="flex items-center gap-3">
          <Link href="/" className="serif text-lg font-semibold text-paper">
            Wisdom<span className="text-brass">Invest</span>
          </Link>
          <span className="rounded-sm border border-hairline px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-[0.16em] text-brass">
            Engine Room
          </span>
        </div>
        <nav className="flex items-center gap-1 text-sm">
          {LINKS.map((l) => {
            const active =
              l.href === "/admin"
                ? pathname === "/admin"
                : pathname.startsWith(l.href);
            return (
              <Link
                key={l.href}
                href={l.href}
                aria-current={active ? "page" : undefined}
                className={`rounded-sm px-3 py-1.5 text-xs font-medium transition-colors ${
                  active
                    ? "bg-panel2 text-brass"
                    : "text-muted hover:text-paper"
                }`}
              >
                {l.label}
              </Link>
            );
          })}
          <Link
            href="/"
            className="ml-2 rounded-sm border border-hairline px-2.5 py-1 text-xs font-medium text-muted transition-colors hover:border-brass hover:text-brass"
          >
            Research Desk
          </Link>
        </nav>
      </div>
    </header>
  );
}
