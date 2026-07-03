import Link from "next/link";

export default function NotFound() {
  return (
    <div className="flex min-h-screen flex-col items-center justify-center gap-4 bg-ink px-6 text-center">
      <span className="num text-5xl font-semibold text-brass">404</span>
      <p className="serif text-xl text-paper">This page is not on the ticker.</p>
      <p className="max-w-sm text-sm text-muted">
        The symbol or route you were looking for doesn&apos;t exist in the
        WisdomInvest terminal.
      </p>
      <Link
        href="/"
        className="rounded-sm border border-hairline bg-panel2 px-4 py-2 text-sm font-medium text-paper transition-colors hover:border-brass hover:text-brass"
      >
        ← Back to Discovery
      </Link>
    </div>
  );
}
