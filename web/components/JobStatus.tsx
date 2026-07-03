import type { JobRun } from "@/lib/types";

const COLORS: Record<JobRun["status"], string> = {
  running: "var(--color-brass)",
  success: "var(--color-sage)",
  error: "var(--color-terracotta)",
};

export function JobStatusDot({ status }: { status: JobRun["status"] }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span
        className="inline-block h-2 w-2 rounded-full"
        style={{ background: COLORS[status] }}
        aria-hidden
      />
      <span
        className="text-[11px] capitalize"
        style={{ color: COLORS[status] }}
      >
        {status}
      </span>
      {status === "running" && (
        <span className="skeleton inline-block h-1 w-6 rounded-full" aria-hidden />
      )}
    </span>
  );
}
