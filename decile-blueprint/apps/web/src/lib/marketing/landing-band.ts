import { FACTOR_COUNT } from "@/lib/marketing/factor-families";
import { HEALTH_UNIVERSES } from "@/lib/market/universes";
import { universeLabelFromName } from "@/lib/screens/universe-label";

/**
 * The band that sits directly under the hero — the scrolling pill rows, the ticker, the four
 * figures and the four categories.
 *
 * **Every figure here is computed from something the code already owns.** docs/14 §Tone allows a
 * claim about *what is published*, never a claim about an outcome, and a hand-typed "2,100+ names"
 * would be neither checkable nor kept in step with the seed. So the numbers are
 * `FACTOR_FAMILIES`' own sum, the length of the published universe list, and the length of the
 * window list — each one changes when the registry changes, which is the only way a marketing
 * figure stays true without anybody remembering to edit it.
 *
 * `src/lib/__tests__/landing-band.test.ts` pins the derivations rather than the rendered strings.
 */

/**
 * The fourteen lists `GET /api/v1/meta/universes` publishes.
 *
 * `HEALTH_UNIVERSES` is the twelve *size bands* — the subset market health draws breadth for. The
 * two below are classifications rather than bands, which is exactly why that module leaves them
 * out, and why they are added back here instead of a third copy of all fourteen being written
 * down. `universe-label.test.ts` holds the transcribed fourteen and is what catches a drift.
 */
const CLASSIFICATION_UNIVERSES = [
  { slug: "nifty-fno", name: "NIFTY FNO" },
  { slug: "etf", name: "All NSE Listed ETFs" },
] as const;

export const INDEX_LISTS: readonly { slug: string; label: string }[] = [
  ...HEALTH_UNIVERSES,
  ...CLASSIFICATION_UNIVERSES,
].map((universe) => ({ slug: universe.slug, label: universeLabelFromName(universe.name) }));

export const INDEX_LIST_COUNT = INDEX_LISTS.length;

/**
 * The five calendar windows every family except skip-month momentum is computed over — docs/13,
 * and the same five the landing copy names in words.
 */
export const RANKING_WINDOWS = ["1 month", "3 months", "6 months", "9 months", "1 year"] as const;

/**
 * The first scrolling row: **what you can actually run here.**
 *
 * It used to scroll the fourteen index names, and the row below it scrolled the factor keys. Both
 * described the data plant — the tables underneath — and neither described the product. A visitor
 * reading "NIFTY Midcap 150 · NIFTY Smallcap 250 · ret_12m" learns that we hold market data, which
 * every competitor also does, and learns nothing about baskets, allocations, brokers or plans.
 *
 * So this row names capabilities and the one below names what they reach. Every entry is a surface
 * that exists in this codebase today; nothing here is a roadmap item wearing the present tense.
 */
export const PRODUCT_PILLS: readonly string[] = [
  "Manager baskets",
  "Build your own screen",
  "Point-in-time backtests",
  "Portfolio allocations",
  "Capital per allocation",
  "Rebalance diffs",
  "Order plans you confirm",
  "Watchlists",
  "Drift tracking",
  "Fee ledger",
  "XIRR",
  "SIP reminders",
];

/**
 * The second row: **what it connects to and what it covers.**
 *
 * Rendered quieter than the row above — outlined rather than filled — so the two read as claim and
 * evidence rather than as one long list broken in half.
 */
export const COVERAGE_PILLS: readonly string[] = [
  "Zerodha Kite",
  "Your own demat",
  "Holdings sync",
  "CSV import",
  "Every NSE-listed equity",
  "ETFs",
  "Point-in-time membership",
  "Corporate actions",
  "Market breadth",
  "Regime",
  "Instrument factsheets",
  "Nightly publish",
];

export interface LandingStat {
  value: string;
  label: string;
}

export const LANDING_STATS: readonly LandingStat[] = [
  { value: String(FACTOR_COUNT), label: "ranking factors, algebra published" },
  { value: String(INDEX_LIST_COUNT), label: "lists of stocks, point-in-time" },
  { value: String(RANKING_WINDOWS.length), label: "calendar windows per family" },
  { value: "₹0", label: "to read a sample screen" },
];

export interface LandingCategory {
  title: string;
  body: string;
}

export const LANDING_CATEGORIES: readonly LandingCategory[] = [
  {
    title: "Screen",
    body: "Rank every listed name on sixty-four published measures, filter what survives, and save the rule so it runs again tomorrow.",
  },
  {
    title: "Baskets",
    body: "Publish a rule as a basket with weights, caps and a written thesis. Subscribers hold the shares directly — nothing is pooled the way a fund pools money.",
  },
  {
    title: "Portfolios",
    body: "One account, many portfolios, and allocations inside them. A momentum basket can sit beside a long-term core and still be read as one position.",
  },
  {
    title: "Brokers",
    body: "Connect the brokers you already use and your holdings sync in. Orders go out as a read-only plan you confirm on the broker's own screen.",
  },
];

/**
 * The ticker's lines.
 *
 * Eight repetitions of one sentence is a loop, not a ticker — the eye catches the repeat on the
 * second pass and stops reading. These are six different facts, so the strip rewards a second look
 * and says six things instead of one. Every line is checkable against the specification, and none
 * of them promises an outcome (docs/14 §Tone, and `copy-lint.test.ts` enforces it).
 */
export const TICKER_LINES: readonly string[] = [
  "Every formula is written down — read the algebra, not a description of it.",
  "A screen run for a past date sees the constituents of that date, never today's.",
  "Rounded once, when it is written — the API, the table and the CSV cannot disagree.",
  "Splits and bonuses are folded in; cash dividends are not. Every return here is a price return.",
  "Where index membership had to be reconstructed, the row says reconstructed.",
  "This site has never placed an order, and cannot.",
];
