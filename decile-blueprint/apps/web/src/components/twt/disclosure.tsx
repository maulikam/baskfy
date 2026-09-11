import type { ReactNode } from "react";

/**
 * An explanation that is closed until somebody wants it, and that says how much is inside.
 *
 * **Why this exists rather than a paragraph.** On 11 Sep 2026 an empty account rendered 4,661
 * pixels on a phone because every explanation on the screen was expanded by default. Each one was
 * true and well written; together they were a wall, and the reader scrolled past all of them. A
 * disclosure that carries its own count and a one-line summary gives the same information the
 * same day — it just does not spend the whole screen on it before anybody has asked.
 *
 * The count is in the summary on purpose: "what the screen passed over (23)" is a fact by itself,
 * and a reader who never opens it has still been told that twenty-three names were passed over.
 */
export function Disclosure({
  summary,
  count,
  children,
  testId,
}: {
  summary: string;
  /** Shown in the summary. Omitted when the thing being disclosed is not a list. */
  count?: number;
  children: ReactNode;
  testId?: string;
}) {
  return (
    <details
      className="rounded-lg border border-border/60 bg-card px-4 py-3"
      {...(testId ? { "data-testid": testId } : {})}
    >
      <summary className="cursor-pointer list-none text-sm font-medium">
        <span className="mr-1.5 text-muted-foreground" aria-hidden="true">
          +
        </span>
        {summary}
        {count === undefined ? null : (
          <span className="ml-1 text-muted-foreground">({count.toLocaleString("en-IN")})</span>
        )}
      </summary>
      <div className="mt-3 max-w-[80ch] space-y-2 text-sm text-muted-foreground">{children}</div>
    </details>
  );
}
