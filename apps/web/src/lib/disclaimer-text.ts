/**
 * The regulatory sentence, and its accessible name.
 *
 * Extracted from `components/data/disclaimer.tsx` so that a **Playwright** sweep can import it:
 * the browser suite runs outside the React build and pulling in a `.tsx` module to read one string
 * would drag React and lucide with it. The component re-exports both, so nothing else changed.
 *
 * The wording is fixed. It is a regulatory statement (docs/11 §"Compliance & legal (India)"), not
 * copy to be tuned, and `e2e/disclaimer-sweep.spec.ts` asserts it verbatim on every analytics
 * route.
 */
export const DISCLAIMER_TEXT =
  "Baskfy is not a SEBI-registered investment adviser. Everything here is factual analysis of " +
  "published market data, not investment advice, and no output is a recommendation to buy or " +
  "sell any security.";

/** The `aria-label` on the landmark. The sweep locates the component by it. */
export const DISCLAIMER_LABEL = "Regulatory disclaimer";
