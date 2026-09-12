"use client";

import Link from "next/link";

/**
 * Marketing-shell error boundary (AUDIT 0.10).
 */
export default function MarketingError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  void error;
  return (
    <div className="mx-auto flex max-w-lg flex-col gap-4 px-5 py-16">
      <h1 className="text-2xl font-light tracking-tight">Something went wrong</h1>
      <p className="text-muted-foreground text-sm leading-relaxed">
        This page could not be loaded. The rest of the site is still available from the header.
      </p>
      <div className="flex flex-wrap gap-3">
        <button
          type="button"
          onClick={reset}
          className="border-border hover:bg-muted rounded-md border px-3 py-1.5 text-sm"
        >
          Try again
        </button>
        <Link href="/" className="text-sm underline-offset-4 hover:underline">
          Go to the home page
        </Link>
      </div>
    </div>
  );
}
