/**
 * Plain English, in one place (M36).
 *
 * Maulik's instruction, 23 Aug 2026: *"If you require to change the label for layman to
 * understand the complexity, make it fun."*
 *
 * ## The rule this module encodes
 *
 * **The visible label is the plain one; the technical one is never deleted.** "Above 200 DMA" is
 * precise and it is also unreadable to anybody who has not traded before. Replacing it with
 * "Above their one-year average" is readable and, on its own, slightly lossy — a 200-day moving
 * average is not exactly a year, and a reader who *does* know the term now cannot tell which
 * measure they are looking at.
 *
 * So both ship. `label` is what the page renders; `term` is the professional name and `plain` is
 * the sentence that explains it, and `<Term/>` puts the second two one hover and one tab-stop
 * away from the first. Nothing is simplified by being hidden, which is the only kind of
 * simplification a page about somebody's money is allowed to do.
 *
 * ## Why the page titles live here too
 *
 * The sidebar, the browser tab, the page heading and the ⌘K palette were four independent copies
 * of the same words, and they had already drifted — the sidebar said "Rebalance Tracker" while
 * the page it opened said "Portfolios". One record per route, read by all four.
 *
 * ## The tone
 *
 * Warm and direct, never chirpy, and never advice. "What you own" is a fact about a database row.
 * "Stocks you should own" would be a recommendation, which docs/11 §Compliance forbids on every
 * surface — `src/lib/__tests__/vocabulary.test.ts` scans this file for that failure mode.
 */

export interface PageVocabulary {
  /** What the sidebar, the heading and the tab all say. */
  readonly title: string;
  /** One line under the heading. What this page answers, in a sentence a beginner can read. */
  readonly blurb: string;
  /** The professional name, where the plain title replaced one. Shown in the sidebar tooltip. */
  readonly formerly?: string;
}

/**
 * Every route the sidebar can reach.
 *
 * Keyed by pathname so `nav.ts`, the page components and the command palette all read the same
 * record. A route with no entry is a route with no agreed name, which is a bug rather than a
 * default.
 */
export const PAGES = {
  "/dashboard": {
    title: "Market today",
    blurb: "Every NSE index, and how each one moved.",
    formerly: "Dashboard",
  },
  "/market-health": {
    title: "Market mood",
    blurb: "Is the whole market rising, or only the headline names?",
    formerly: "Market Health",
  },
  "/screens": {
    title: "Stock finder",
    blurb: "Set the rules you care about, and see which stocks meet them.",
    formerly: "Screens",
  },
  "/portfolios": {
    title: "My portfolios",
    blurb: "What you hold, and what would change if you rebalanced today.",
    formerly: "Rebalance Tracker",
  },
  "/explore": {
    title: "Explore",
    blurb: "Browse curated baskets by cost, swing, and how they have moved so far.",
  },
  "/investments": {
    title: "Investments",
    blurb: "Baskets you hold, what they are worth, and what still needs a decision.",
  },
  "/watchlist": {
    title: "Watchlist",
    blurb: "Baskets you are keeping an eye on, and how they have moved since you added them.",
  },
  "/fees": {
    title: "Fees",
    blurb: "Platform fees accrued on invest batches — journaled, not collected this run.",
  },
  "/create": {
    title: "Create a basket",
    blurb: "Pick at least two stocks, set weights, and save a private basket only you can see.",
  },
  "/baskets": {
    title: "Today's basket",
    blurb: "The names the strategy ranks highest right now, and the weight each would carry.",
    formerly: "Baskets",
  },
  "/backtests": {
    title: "Time machine",
    blurb: "Run a set of rules against the past and see how it would have gone.",
    formerly: "Backtests",
  },
  "/listings": {
    title: "New listings",
    blurb: "Companies that have recently joined the exchange.",
    formerly: "Listings",
  },
  "/performance": {
    title: "Returns",
    blurb: "How the money actually being traded has done.",
    formerly: "Performance",
  },
  "/holdings": {
    title: "What you own",
    blurb: "Every position on the desk right now, and what it is worth.",
    formerly: "Holdings",
  },
  "/tradebook": {
    title: "Trade history",
    blurb: "Every buy and sell, oldest to newest.",
    formerly: "Trades",
  },
  "/regime": {
    title: "Risk dial",
    blurb: "How much of the portfolio the strategy is willing to hold in shares this week.",
    formerly: "Market stance",
  },
  "/reconcile": {
    title: "Planned vs done",
    blurb: "What the plan asked for, next to what the broker actually filled.",
    formerly: "Plan vs fills",
  },
  "/pricing": { title: "Plans", blurb: "What each plan includes.", formerly: "Pricing" },
  "/invoices": { title: "Invoices", blurb: "Your tax invoices, ready to download." },
  "/profile": { title: "Profile", blurb: "Your name, your email, and your data." },
  "/brokers": {
    title: "Brokers",
    blurb: "Connect Zerodha, HDFC, Kotak, ICICI and more in a few clicks.",
    formerly: "Broker connections",
  },
  "/change-password": {
    title: "Password",
    blurb: "Change the password you sign in with.",
    formerly: "Change Password",
  },
  "/faq": {
    title: "Common questions",
    blurb: "The things people ask before they start.",
    formerly: "FAQ",
  },
  "/blog": { title: "Blog", blurb: "What we have been working on." },
  "/support": { title: "Get help", blurb: "Tell us what went wrong and we will look.", formerly: "Support" },
} as const satisfies Record<string, PageVocabulary>;

export type PagePath = keyof typeof PAGES;

export interface TermVocabulary {
  /** The plain-English label the page renders. */
  readonly label: string;
  /** The professional name, kept so an experienced reader can still identify the measure. */
  readonly term: string;
  /** One sentence. What it measures and why somebody would look at it. */
  readonly plain: string;
}

/**
 * The measures a page puts in front of somebody, in the words they would use themselves.
 *
 * These are the ones an ordinary reader meets on a first visit. Deliberately not a glossary of
 * all 64 factors: a term earns an entry here by appearing on a page, and the screener's factor
 * catalogue has its own descriptions in `factor_registry.py`.
 */
export const TERMS = {
  above_200dma: {
    label: "Above their one-year trend",
    term: "Above the 200-day moving average",
    plain:
      "The share of stocks trading above their own average price of the last 200 trading days — roughly a year. A high number means most companies are in an uptrend, not just the big ones.",
  },
  above_50dma: {
    label: "Above their three-month trend",
    term: "Above the 50-day moving average",
    plain:
      "The same idea over 50 trading days — about a quarter. It turns faster than the one-year line, so it moves first when the mood changes.",
  },
  near_high: {
    label: "Near their highest ever",
    term: "Within 10% of the all-time high",
    plain:
      "The share of stocks trading within a tenth of the highest price they have ever reached. Rising means strength is broad; falling means the leaders are carrying the index alone.",
  },
  positive_1y: {
    label: "Up over the past year",
    term: "1-year return above 0%",
    plain: "The share of stocks worth more today than they were twelve months ago.",
  },
  breadth: {
    label: "How many are joining in",
    term: "Market breadth",
    plain:
      "Whether a rise is shared by most companies or driven by a handful of large ones. An index can climb while most of its members fall.",
  },
  universe: {
    label: "Which list of stocks",
    term: "Universe",
    plain:
      "The group being measured — NIFTY 50 is the fifty largest, NIFTY 500 is a much wider slice of the market.",
  },
  notional: {
    label: "Total being invested",
    term: "Notional",
    plain: "The full amount the basket is sized against, before anything is bought.",
  },
  momentum_score: {
    label: "Momentum score",
    term: "Composite momentum rank",
    plain:
      "How strongly a stock has been rising compared with the others in the list, on a 0–100 scale. It describes the past; it does not predict the future.",
  },
  stop_price: {
    label: "Auto-sell price",
    term: "GTT stop",
    plain:
      "The price at which a standing order would sell the position, placed so a single bad run cannot become a large loss. It is set from how much the stock normally moves.",
  },
  weight: {
    label: "Share of the basket",
    term: "Target weight",
    plain: "How much of the total each name is allotted. Equal weights mean every name gets the same rupees.",
  },
  pe: {
    label: "Price vs profit",
    term: "P/E ratio",
    plain:
      "The price divided by a year of earnings. Higher means investors are paying more for each rupee the company earns.",
  },
  pb: {
    label: "Price vs book value",
    term: "P/B ratio",
    plain: "The price divided by what the company's net assets are worth on paper.",
  },
  div_yield: {
    label: "Dividend",
    term: "Dividend yield",
    plain: "The dividend paid over a year, as a percentage of today's price.",
  },
  regime_tier: {
    label: "Risk dial",
    term: "Regime tier",
    plain:
      "A weekly reading of how calm or stressed the market is. Calmer readings let the strategy hold more in shares; stressed ones hold more in cash.",
  },
  drawdown: {
    label: "Worst drop so far",
    term: "Maximum drawdown",
    plain:
      "The largest fall from a peak to the low that followed it. It is the number that tells you what holding this would have felt like.",
  },
  cagr: {
    label: "Yearly growth rate",
    term: "CAGR",
    plain: "The steady annual rate that would turn the starting value into the ending one.",
  },
  sharpe: {
    label: "Return for the bumpiness",
    term: "Sharpe ratio",
    plain: "How much return was earned for each unit of ups and downs endured. Higher is steadier.",
  },
  score_parts: {
    label: "Score breakdown",
    term: "Component scores",
    plain:
      "The six parts the momentum score is built from, in order: trend, momentum, steadiness, consistency, how easily the stock trades, and a penalty for anything unusual. Higher is stronger in each, except the last.",
  },
  cash_target: {
    label: "Kept as cash",
    term: "Cash target",
    plain:
      "The slice deliberately left uninvested. It rises when the market is stressed and falls when it is calm.",
  },
  turnover: {
    label: "How much changes hands",
    term: "Turnover",
    plain: "The share of the portfolio that gets replaced at each rebalance. More turnover means more brokerage.",
  },
} as const satisfies Record<string, TermVocabulary>;

export type TermId = keyof typeof TERMS;
