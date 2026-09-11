/**
 * `docs/twt/01` §8 — **a component, above the numbers, never a footer** (house rule 9's second
 * half, which `05` §3 restates for this page by name).
 *
 * The placement is the whole point. These are not fine print about a result; they are the
 * conditions under which the result means anything at all, and a reader who has already seen
 * "20.9%" before reading them has formed the belief these paragraphs exist to prevent.
 *
 * WHAT IS VERBATIM AND WHAT IS NOT
 * --------------------------------
 * Every **number and every quantity** is the study's own, unrounded and unsoftened: 164 trades,
 * ten of them 53% of gross profit, roughly fifteen independent observations, the 20% give-back,
 * ₹10 lakh, ₹25 lakh. Those are the substance, and `05` §3 requires them on the page.
 *
 * The one phrase deliberately not transcribed is `05` §3's "no backtest **in this repository**".
 * A repository is a fact about where the code is kept, and the rule this product learned on
 * 11 Sep 2026 is that nothing from the inside of the system reaches a reader's eyes. The sentence
 * says the same thing about the same set of runs — every result behind this page — without
 * naming the machinery. DECISIONS-TW TW8.2.
 */
export function BacktestCaveats() {
  return (
    <aside
      aria-labelledby="twt-caveats-heading"
      data-testid="twt-caveats"
      className="rounded-lg border border-warning/40 bg-warning-muted p-4"
    >
      <h2
        id="twt-caveats-heading"
        className="text-sm font-semibold uppercase tracking-wide text-warning"
      >
        Read this before the numbers
      </h2>
      <div className="mt-2 max-w-[80ch] space-y-2 text-sm leading-relaxed text-foreground">
        <p data-testid="twt-caveat-trades">
          <strong>164 trades.</strong> Ten of them are 53% of the gross profit, and the single best
          one is 11% of it. Three separate years carry the annual return on twelve to fifteen
          trades each. With an average hold of about a year, the nine-year test contains perhaps{" "}
          <strong>fifteen independent observations</strong> — read the headline return as an edge
          with the right sign and a wide range around it, not as a forecast.
        </p>
        <p data-testid="twt-caveat-giveback">
          <strong>The trail is the strategy, and it is slow.</strong> The average position is held
          about 105 sessions and the longest was 601. The stop follows the highest price since
          buying, 20% below it, so <strong>a 20% give-back from the peak is routine</strong>, not a
          failure: on a ₹2.5 lakh position that is a ₹50,000 open loss this strategy will sit
          through on the way to its result.
        </p>
        <p data-testid="twt-caveat-history">
          <strong>One history, and a favourable one.</strong> The stronger half of the test is
          2023&ndash;24; the weaker, earlier half is the better guide to a normal decade. Fills are
          modelled rather than experienced, at 25 basis points a side, with no interest earned on
          idle cash and sparse corporate-action data before 2024. And the screen measured here is
          the one visible at the close, which names fewer stocks on some days than the published
          historical version of the same screen.
        </p>
        <p data-testid="twt-caveat-capital">
          <strong>One thing none of these runs has measured: this strategy at ₹25 lakh.</strong>{" "}
          Every result on this page was produced at ₹10 lakh, where the limit on taking more than a
          small share of a name&rsquo;s daily trading never binds on anything. The first position
          taken with real money would be the first one that limit has ever bound.
        </p>
      </div>
    </aside>
  );
}
