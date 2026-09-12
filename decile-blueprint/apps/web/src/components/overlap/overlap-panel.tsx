import Link from "next/link";
import type { Route } from "next";

import type { Intersection } from "@/lib/overlap/overlap";
import { cn } from "@/lib/utils";

/**
 * One of the overlap panels on `/build/overlap`.
 *
 * A list of symbols with a one-line summary. Links go to the instrument factsheet so a name on
 * both scans can be opened without leaving the product for a charting site.
 */
export function OverlapPanel({
  title,
  blurb,
  result,
  testId,
}: {
  title: string;
  blurb: string;
  result: Intersection;
  testId: string;
}) {
  return (
    <section
      className="space-y-3 rounded-xl border border-border/70 bg-card/40 p-5"
      data-testid={testId}
      aria-labelledby={`${testId}-heading`}
    >
      <div className="space-y-1">
        <h2
          id={`${testId}-heading`}
          className="text-sm font-medium uppercase tracking-wide text-muted-foreground"
        >
          {title}
        </h2>
        <p className="max-w-[72ch] text-sm leading-relaxed text-muted-foreground">{blurb}</p>
      </div>

      <p
        className="text-sm leading-relaxed"
        data-testid={`${testId}-summary`}
        data-available={result.available ? "true" : "false"}
      >
        {result.summary}
      </p>

      {!result.available ? null : result.sharedCount === 0 ? (
        <p className="text-sm text-muted-foreground" data-testid={`${testId}-empty`}>
          No names in common on this session.
        </p>
      ) : (
        <ul
          className="grid gap-x-6 gap-y-1 sm:grid-cols-2 lg:grid-cols-3"
          data-testid={`${testId}-list`}
        >
          {result.shared.map((symbol) => (
            <li key={symbol} className="border-b border-border/40 py-1.5 text-sm">
              <Link
                href={`/instruments/${encodeURIComponent(symbol)}` as Route}
                className={cn(
                  "font-medium tabular-nums text-foreground underline-offset-4 hover:underline",
                )}
              >
                {symbol}
              </Link>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
