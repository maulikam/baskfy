"use client";

/**
 * What a sleeve page shows when the read did not fail *and* did not succeed — it was refused,
 * the deployment is degraded, or the API errored.
 *
 * `gates/sleeve-read-contract.md` C7. Every sleeve used to render all three of those as its
 * empty state: "No flags today", "Nothing has been read for this strategy yet". That sentence
 * over a database holding 157 setups is the bug this whole audit is named after, and it is worse
 * than an error page because it is *believed* — a person reads it and concludes the job did not
 * run.
 *
 * **The honest limit of this component.** Next redacts a server-thrown error's message before it
 * reaches the browser in production, so this boundary cannot say *which* of the three it was.
 * What it can say — and what matters — is that the page is not claiming the book is empty. The
 * follow-up is for each page to catch `SleeveUnavailableError` from its own read and render the
 * reason inline, where the message never leaves the server.
 */
export function SleeveUnavailable({
  strategy,
  reset,
}: {
  /** The strategy this page was reading, named the way `lib/vocabulary` names it. */
  strategy: string;
  reset: () => void;
}) {
  return (
    <main className="mx-auto flex max-w-2xl flex-col gap-4 px-4 py-16">
      <h1 className="text-2xl font-light tracking-tight">{strategy} could not be read</h1>
      <p className="text-muted-foreground text-sm leading-relaxed">
        This is <strong>not</strong> the same thing as &ldquo;nothing has been detected&rdquo;.
        The read was refused, or the service could not answer — so the page is saying so rather
        than showing you a quiet day that may not be quiet.
      </p>
      <p className="text-muted-foreground text-sm leading-relaxed">
        If this keeps happening, the reason is in the API&rsquo;s log against this request: a
        second account reading a single-tenant deployment, a deployment with no tenant
        configured, or a server error.
      </p>
      <div>
        <button
          type="button"
          onClick={reset}
          className="border-border hover:bg-muted rounded-md border px-3 py-1.5 text-sm"
        >
          Try again
        </button>
      </div>
    </main>
  );
}
