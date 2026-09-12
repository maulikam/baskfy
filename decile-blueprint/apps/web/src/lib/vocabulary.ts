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
  /* —— The signed-in landing surface (SC9, docs/smallcase/05 §6.1) —— */
  "/home": {
    title: "Home",
    blurb: "What you hold, what needs a decision, and what is worth a look.",
  },
  /* —— Tree 6 canonical hubs (label = route section = tab title) —— */
  "/market/today": {
    title: "Today · Market",
    blurb: "Every NSE index, and how each one moved.",
    formerly: "Dashboard",
  },
  "/market/mood": {
    title: "Mood · Market",
    blurb: "Is the whole market rising, or only the headline names?",
    formerly: "Market Health",
  },
  "/market/listings": {
    title: "Listings · Market",
    blurb: "Companies that have recently joined the exchange.",
    formerly: "Listings",
  },
  /* —— The swing hub (SW4, docs/swing/05 §2). Read-only: it shows what the detectors found
     and what the tape allows. A line becomes an order in the desk console and nowhere else. —— */
  "/swing": {
    title: "Swing",
    blurb: "Which leaders have finished resting, where they would break out, and where the stop goes.",
  },
  "/swing/watchlist": {
    title: "Watchlist · Swing",
    blurb: "The names you are waiting on, and the level each one has to clear.",
  },
  "/swing/positions": {
    title: "Positions · Swing",
    blurb: "What is open, where its stop is, and what the rules want done at tomorrow's open.",
  },
  "/swing/market": {
    title: "Market · Swing",
    blurb: "Whether the tape is worth trading, and how much the allocation may carry.",
  },
  "/swing/journal": {
    title: "Journal · Swing",
    blurb: "What the closed trades add up to in R, and whether the ladder says press or sit.",
  },
  /* —— The volume-breakout hub (VB8, docs/vbt/05 §2). Read-only, like the swing hub and for
     the same reason: every order here is confirmed by hand, in the desk console. —— */
  "/vbt": {
    title: "Volume breakout",
    blurb: "Which names printed a real volume breakout, what the tape allows, and where the limit sits.",
  },
  "/vbt/book": {
    title: "Positions · Volume breakout",
    blurb: "The limits still working, what is open with its stop, and how often the fills arrive.",
  },
  "/vbt/backtest": {
    title: "Backtest · Volume breakout",
    blurb: "What the rule did over nine years, with the caveats before the numbers.",
  },
  /* —— The three-weeks-tight hub (TW8, docs/twt/05 §1). Read-only, like the other two sleeve
     hubs and for the same reason: a plan line becomes an order in the desk console, on a click,
     and nowhere else (docs/twt/02 Track C §4). "Three weeks tight" is the pattern's own name —
     O'Neil's, not this product's coinage — so it is what a reader is shown. —— */
  "/twt": {
    title: "Three weeks tight",
    blurb: "Which names have gone quiet after a run, and where every open stop is sitting.",
  },
  "/twt/backtest": {
    title: "Backtest · Three weeks tight",
    blurb: "What the rule did over nine years, with the conditions on it read first.",
  },
  /* The settings live under Me, not the hub (docs/swing/05 §2): they are about the person's
     money and limits, not today's tape. */
  "/me/swing": {
    title: "Swing settings",
    blurb: "How much the swing allocation may risk, and where the server's ceilings sit.",
  },
  "/discover": {
    title: "Discover",
    blurb: "Say how you want to invest, and see which baskets match it.",
    formerly: "Explore",
  },
  "/discover/all": {
    title: "All baskets · Discover",
    blurb: "Every published basket, with its risk before its return.",
    formerly: "Explore",
  },
  "/discover/search": {
    title: "Search · Discover",
    blurb: "Find a basket by name, manager or category — the catalogue filtered as you type.",
  },
  "/discover/compare": {
    title: "Compare · Discover",
    blurb: "Two or three baskets side by side, measured over the same period.",
  },
  "/discover/collections": {
    title: "Collections · Discover",
    blurb: "Baskets grouped into shelves — by cost of entry, by how they pick, by who runs them.",
  },
  "/discover/saved": {
    title: "Saved · Discover",
    blurb: "Baskets you have saved, and how they have moved since you saved them.",
  },
  "/discover/featured": {
    /*
      Renamed. "Featured" implied an editorial choice among many and the page has never been one:
      it renders the house momentum strategy as it would be constructed today. A label that
      cannot be explained is worse than a plain one — docs/DISCOVER-AUDIT.md C13.
    */
    title: "House strategy · Discover",
    blurb: "The engine's own momentum basket, as it would be built today — names and weights.",
    formerly: "Today's basket",
  },
  "/build": {
    title: "Screens · Build",
    blurb: "Set the rules you care about, and see which stocks meet them.",
    formerly: "Screens",
  },
  "/build/backtests": {
    title: "Backtests · Build",
    blurb: "Run a set of rules against the past and see how it would have gone.",
    formerly: "Backtests",
  },
  "/build/overlap": {
    title: "Overlap · Build",
    blurb:
      "Which names sit on Volume breakout and Three weeks tight together — and which of those also clear a screen.",
  },
  /* —— The Portfolio hub (PORTFOLIO_REDESIGN.md §2) ——
     Five tabs, one section. `Overview` is the landing tab; `Portfolios` absorbed the tab that
     used to be called `Investments`, because they named the same thing (§1 problem 1). Titles
     carry the section so the browser tab, the ⌘K palette and the heading all agree. —— */
  "/portfolio/overview": {
    title: "Overview · Portfolio",
    blurb: "Everything you hold, across baskets and brokers, in one picture.",
    formerly: "Investments",
  },
  "/portfolio/portfolios": {
    title: "Portfolios · Portfolio",
    blurb: "Your holdings grouped into portfolios that can be measured on their own.",
    formerly: "Rebalance Tracker",
  },
  "/portfolio/holdings": {
    title: "Holdings · Portfolio",
    blurb: "Every share you own, which broker holds it, and which portfolio it belongs to.",
  },
  "/portfolio/activity": {
    title: "Activity · Portfolio",
    blurb: "Buys, sells, cash moves, dividends and corporate actions, newest first.",
  },
  "/portfolio/watchlist": {
    title: "Watchlist · Portfolio",
    blurb: "Baskets you are keeping an eye on, and how they have moved since you added them.",
  },
  /* —— Legacy path keys kept so redirects and residual imports still resolve —— */
  "/me/investments": {
    title: "Investments · Me",
    blurb: "Baskets you hold, what they are worth, and what still needs a decision.",
  },
  "/me/portfolios": {
    title: "Portfolios · Me",
    blurb: "Each basket, screen, and holding in its own portfolio, with its own capital.",
    formerly: "Rebalance Tracker",
  },
  "/me/watchlist": {
    title: "Watchlist · Me",
    blurb: "Baskets you are keeping an eye on, and how they have moved since you added them.",
  },
  "/dashboard": {
    title: "Today · Market",
    blurb: "Every NSE index, and how each one moved.",
    formerly: "Dashboard",
  },
  "/market-health": {
    title: "Mood · Market",
    blurb: "Is the whole market rising, or only the headline names?",
    formerly: "Market Health",
  },
  "/screens": {
    title: "Screens · Build",
    blurb: "Set the rules you care about, and see which stocks meet them.",
    formerly: "Screens",
  },
  "/portfolios": {
    title: "Portfolios · Me",
    blurb: "Each basket, screen, and holding in its own portfolio, with its own capital.",
    formerly: "Rebalance Tracker",
  },
  "/explore": {
    title: "Explore · Baskets",
    blurb: "Browse curated baskets by cost, volatility, and how they have moved so far.",
  },
  "/investments": {
    title: "Investments · Me",
    blurb: "Baskets you hold, what they are worth, and what still needs a decision.",
  },
  "/watchlist": {
    title: "Watchlist · Me",
    blurb: "Baskets you are keeping an eye on, and how they have moved since you added them.",
  },
  "/fees": {
    title: "Fees",
    blurb: "Platform fees accrued on invest batches — journaled, not collected this run.",
  },
  "/create": {
    title: "Create a basket",
    blurb: "Pick a screen — templates are there on first login — then set the amount and how many names.",
  },
  "/backtests": {
    title: "Backtests · Build",
    blurb: "Run a set of rules against the past and see how it would have gone.",
    formerly: "Backtests",
  },
  "/listings": {
    title: "Listings · Market",
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
    blurb: "Zerodha Kite hand-off is live. Other brokers are listed as planned, not ready.",
    formerly: "Broker connections",
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
