/**
 * Authenticated-shell loading fallback so navigations under `(app)` are not a blank flash
 * (AUDIT 0.10).
 */
export default function AppLoading() {
  return (
    <div className="flex flex-col gap-4 py-8" aria-busy="true" aria-live="polite">
      <div className="bg-muted h-8 w-48 animate-pulse rounded-md" />
      <div className="bg-muted h-4 w-full max-w-md animate-pulse rounded-md" />
      <div className="bg-muted h-4 w-full max-w-sm animate-pulse rounded-md" />
      <span className="sr-only">Loading</span>
    </div>
  );
}
