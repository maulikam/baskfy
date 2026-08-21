import type { RebalanceOut } from "@baskfy/api-client";

/**
 * PROMPTS.md Prompt 14 §3: "plus an explanation of the buffer rule".
 *
 * Written with the user's own numbers substituted in, because the rule is only confusing in the
 * abstract: "hold until rank > N + buffer" is a sentence people nod at and then misread, whereas
 * "held names stay while they rank 1–30, and leave at 31" is a sentence they can check against
 * the columns next to it.
 *
 * The claim about turnover is docs/01 §8's, not ours: "which materially reduces turnover".
 */
export interface BufferExplainerProps {
  topN: number;
  holdBuffer: number;
  result?: RebalanceOut | null;
}

export function BufferExplainer({ topN, holdBuffer, result }: BufferExplainerProps) {
  const limit = topN + holdBuffer;
  return (
    <aside
      className="rounded-md border border-border bg-muted/40 p-4 text-sm"
      data-testid="buffer-explainer"
      aria-label="How the buffer rule works"
    >
      <h2 className="text-sm font-semibold tracking-tight">How this is decided</h2>
      <ul className="mt-2 space-y-1 text-muted-foreground">
        <li>
          <strong className="text-foreground">Entries</strong> — ranked 1–{topN} in the screen and
          not currently held.
        </li>
        <li>
          <strong className="text-foreground">Inside WRH</strong> — held, ranked{" "}
          {topN + 1}–{limit}. Still inside the “within rebalance hold” band, so keep them.
        </li>
        <li>
          <strong className="text-foreground">Exits</strong> — held and ranked {limit + 1} or worse,
          or gone from the screen entirely.
        </li>
      </ul>
      <p className="mt-3 text-muted-foreground">
        Buying the top {topN} and holding until a name falls past {limit} is the rank-buffer rule.
        The band is hysteresis: without it, a stock oscillating around rank {topN} is bought and
        sold every month. It materially reduces turnover.
      </p>
      {result ? (
        <p className="mt-3 text-xs text-muted-foreground">
          Computed against {result.screen_result_count} screened names as of {result.as_of}, on
          data version {result.data_version}.
          {result.delisted_count > 0
            ? ` ${result.delisted_count} holding${result.delisted_count === 1 ? " is" : "s are"} delisted and must be exited regardless of rank.`
            : ""}
        </p>
      ) : null}
    </aside>
  );
}
