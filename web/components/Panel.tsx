import type { ReactNode } from "react";

// Hairline-ruled panel — the base surface of the terminal.
export function Panel({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <section
      className={`rounded-md border border-hairline bg-panel ${className}`}
    >
      {children}
    </section>
  );
}

export function PanelHeader({
  title,
  hint,
  right,
  editorial = false,
}: {
  title: ReactNode;
  hint?: ReactNode;
  right?: ReactNode;
  editorial?: boolean;
}) {
  return (
    <header className="flex items-baseline justify-between gap-3 border-b border-hairline px-4 py-2.5">
      <div className="flex items-baseline gap-3">
        <h2
          className={
            editorial
              ? "serif text-lg font-medium text-paper"
              : "text-xs font-semibold uppercase tracking-[0.14em] text-muted"
          }
        >
          {title}
        </h2>
        {hint && <span className="text-xs text-muted">{hint}</span>}
      </div>
      {right}
    </header>
  );
}
