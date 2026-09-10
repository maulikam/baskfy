/**
 * `docs/vbt/01` §5, verbatim — **a component, above the numbers, never a footer** (house rule 9).
 *
 * The placement is the point. These caveats are not fine print about a number; they are the
 * conditions under which the number means anything at all, and a reader who sees 18.2% before
 * reading them has already formed the belief the caveats exist to prevent.
 *
 * The text is the study's own, transcribed once, here. `01` §5 is the source; when they disagree
 * the document is the original and this is the stale half.
 */
export function BacktestCaveats() {
  return (
    <aside
      aria-labelledby="vbt-caveats-heading"
      data-testid="vbt-caveats"
      className="rounded-lg border border-amber-600/40 bg-amber-50/50 p-4 dark:bg-amber-950/20"
    >
      <h2
        id="vbt-caveats-heading"
        className="text-sm font-semibold uppercase tracking-wide text-amber-800 dark:text-amber-300"
      >
        This is one history, and it is a favourable one
      </h2>
      <div className="mt-2 max-w-[80ch] space-y-2 text-sm text-foreground/90">
        <p>
          2020&ndash;24 was the best four-year run Indian small and mid caps
          have had. The in-sample half (12.6%) is a better guide to a normal
          decade than the out-of-sample half (26%). The split is{" "}
          <strong>not a true walk-forward</strong> &mdash; the filters were
          chosen looking at both halves, then required to hold in each &mdash;
          so treat the out-of-sample number as &ldquo;did not break&rdquo;, not
          as an unbiased forecast.
        </p>
        <p>
          Nine years and one bear market (2018&ndash;20) is thin evidence for a
          regime gate. Fills are modelled, not experienced: limit fills at the
          limit, stops at the stop unless gapped, 25 bps a side. The strategy is
          long-only, cash otherwise, no interest on cash (which would add about
          2 points to CAGR at 37% idle). Corporate actions before 2024 are as
          the source adjusted them.
        </p>
        <p>
          And one the implementation adds rather than the study:{" "}
          <strong>trading live, there is no intraday data.</strong> A limit the
          research assumes filled when the day&rsquo;s low touched it is, in
          life, an order resting at the exchange that may or may not fill at
          that price.
        </p>
      </div>
    </aside>
  );
}
