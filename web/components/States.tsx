import type { ReactNode } from "react";

export function Skeleton({
  className = "",
  style,
}: {
  className?: string;
  style?: React.CSSProperties;
}) {
  return <div className={`skeleton ${className}`} style={style} />;
}

export function TableSkeleton({ rows = 12 }: { rows?: number }) {
  return (
    <div className="space-y-px p-2" aria-hidden>
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="flex items-center gap-3 px-2 py-2">
          <Skeleton style={{ width: 24, height: 12 }} />
          <Skeleton style={{ width: 110, height: 10 }} />
          <Skeleton style={{ width: 60, height: 12 }} />
          <Skeleton className="flex-1" style={{ height: 12 }} />
          <Skeleton style={{ width: 48, height: 12 }} />
        </div>
      ))}
    </div>
  );
}

export function ErrorState({
  message,
  onRetry,
}: {
  message: string;
  onRetry?: () => void;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 px-6 py-14 text-center">
      <div className="text-terracotta" aria-hidden>
        <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
          <path d="M12 8v5M12 16.5v.5" strokeLinecap="round" />
          <path d="M10.3 3.9 2.4 18a2 2 0 0 0 1.7 3h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" />
        </svg>
      </div>
      <p className="max-w-md text-sm text-muted">{message}</p>
      {onRetry && (
        <button
          onClick={onRetry}
          className="rounded-sm border border-hairline bg-panel2 px-3 py-1.5 text-xs font-medium text-paper transition-colors hover:border-brass hover:text-brass"
        >
          Retry
        </button>
      )}
    </div>
  );
}

export function EmptyState({
  title,
  hint,
  children,
}: {
  title: string;
  hint?: string;
  children?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-6 py-14 text-center">
      <p className="serif text-lg text-paper">{title}</p>
      {hint && <p className="max-w-md text-sm text-muted">{hint}</p>}
      {children}
    </div>
  );
}
