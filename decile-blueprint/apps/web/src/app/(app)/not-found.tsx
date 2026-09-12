import Link from "next/link";

/**
 * Authenticated-shell 404. Without this, `notFound()` and unknown URLs under `(app)` render
 * Next's unshelled 404 with no nav (AUDIT 0.10).
 */
export default function AppNotFound() {
  return (
    <div className="mx-auto flex max-w-lg flex-col gap-4 py-8">
      <h1 className="text-2xl font-light tracking-tight">Page not found</h1>
      <p className="text-muted-foreground text-sm leading-relaxed">
        That address is not a page in Baskfy. Use the navigation above, or go home.
      </p>
      <Link href="/home" className="text-sm underline-offset-4 hover:underline">
        Go to Home
      </Link>
    </div>
  );
}
