/**
 * Lightweight Suspense fallback for Tree-5 list/shell RSC pages.
 * Lets the app shell paint while the page's timed API hop resolves (or aborts).
 */
export function RscListFallback({ label = "Loading…" }: { label?: string }) {
  return (
    <div
      role="status"
      aria-live="polite"
      className="grid min-h-[12rem] place-items-center rounded-xl border border-dashed border-border bg-card/40 px-6 py-12 text-sm text-muted-foreground"
    >
      {label}
    </div>
  );
}
