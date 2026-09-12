"use client";

/**
 * Root error boundary — replaces the root layout when it fails, so it must render its own
 * `<html>` / `<body>` (AUDIT 0.10).
 */
export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  void error;
  return (
    <html lang="en-IN">
      <body className="antialiased">
        <main className="mx-auto flex min-h-dvh max-w-lg flex-col justify-center gap-4 px-5 py-16">
          <h1 className="text-2xl font-light tracking-tight">Something went wrong</h1>
          <p className="text-sm leading-relaxed text-neutral-600">
            Baskfy could not render this page. Try again, or reload.
          </p>
          <button
            type="button"
            onClick={reset}
            className="w-fit rounded-md border border-neutral-300 px-3 py-1.5 text-sm hover:bg-neutral-50"
          >
            Try again
          </button>
        </main>
      </body>
    </html>
  );
}
