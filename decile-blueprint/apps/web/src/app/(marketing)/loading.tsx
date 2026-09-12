/**
 * Marketing-shell loading fallback (AUDIT 0.10).
 */
export default function MarketingLoading() {
  return (
    <div className="flex flex-col gap-4 px-5 py-16" aria-busy="true" aria-live="polite">
      <div className="bg-muted h-8 w-48 animate-pulse rounded-md" />
      <div className="bg-muted h-4 w-full max-w-md animate-pulse rounded-md" />
      <div className="bg-muted h-4 w-full max-w-sm animate-pulse rounded-md" />
      <span className="sr-only">Loading</span>
    </div>
  );
}
