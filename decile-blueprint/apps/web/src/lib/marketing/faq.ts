/**
 * The FAQ question set — Prompt 18 deliverable 2: "`/faq` with the reference product's question
 * set answered for our product".
 *
 * ## Where these questions come from
 *
 * `docs/01` §1 records that the reference product has a `/faq` route. It does **not** capture the
 * questions on it, and nothing else in the bundle does either. Rather than invent a question set
 * and attribute it to the reference product, every question below is derived from something
 * `docs/01` observed *directly* — the universe list (§2.1), the sentinel conventions (§2.4–§2.6),
 * the multi-factor ranking algorithm quoted verbatim from the site's own documentation (§2.12),
 * the "historical data is available from 1 Nov 2024" statement (§2.13), the gated feature list and
 * the SEBI disclaimer (§1) — plus the questions this build's own open items make unavoidable.
 * `docs/DECISIONS.md` §18.4 records the choice.
 *
 * ## The answers are allowed to be unflattering
 *
 * docs/14 §Tone: "The product's credibility comes from showing its work." Three of the answers
 * below say a thing does not work yet. That is the point of having them: a visitor who finds out
 * about the empty P/E column from the product is annoyed once, and a visitor who finds out from
 * the FAQ before signing up is not.
 */

export interface FaqEntry {
  /** Stable anchor, so a support reply can link to one answer. */
  id: string;
  question: string;
  /** Paragraphs. Plain strings — this feeds both the page and its `FAQPage` JSON-LD. */
  answer: string[];
}

export interface FaqSection {
  heading: string;
  entries: FaqEntry[];
}

export const FAQ_SECTIONS: readonly FaqSection[] = [
  {
    heading: "What this is",
    entries: [
      {
        id: "what-is-baskfy",
        question: "What does Baskfy actually do?",
        answer: [
          "Every night after the NSE session, Baskfy computes sixty-four ranking factors for every listed equity and stores one wide row per instrument per date. A screen is then a filter and a sort over that table: pick a universe, pick a factor, add as many of the twenty-odd filters as you want, and read the ranking.",
          "It is a measurement tool. It tells you how a set of names ranks on a stated formula on a stated date. It does not tell you what to do with that.",
        ],
      },
      {
        id: "not-an-adviser",
        question: "Is this investment advice?",
        answer: [
          "No. Baskfy is not a SEBI-registered investment adviser and does not publish advice, recommendations, target prices or model portfolios. Every output is arithmetic over published market data.",
          "The disclaimer page sets this out at length, and the same statement is rendered on every screen in the product rather than tucked into a footer.",
        ],
      },
      {
        id: "who-is-it-for",
        question: "Who is it for?",
        answer: [
          "People who already run a rules-based momentum process and want the ranking computed correctly, reproducibly and with the formula visible. If you want a tip sheet, this is the wrong product and will disappoint you.",
        ],
      },
    ],
  },
  {
    heading: "The data",
    entries: [
      {
        id: "universes",
        question: "Which universes can I screen?",
        answer: [
          "Fourteen: NIFTY 50, NEXT 50, 100, 200, 500, TOTAL MARKET, LARGE MID 250, MIDCAP 150, SMALLCAP 250, MICROCAP 250, MID SMALL 400, FNO, all NSE listed stocks, and all NSE listed ETFs.",
          "Membership is point-in-time. A screen run for a past date uses the constituents that were in the index on that date.",
        ],
      },
      {
        id: "how-far-back",
        question: "How far back does the history go?",
        answer: [
          "Historical screen runs are available from 1 November 2024, which is where the published factor history starts. Backtests read the same table and are bounded by the same date.",
          "A longer backfill is the single biggest open item on this build. Until it has run, a backtest that asks for a period before that date will fail rather than silently truncate.",
        ],
      },
      {
        id: "adjusted",
        question: "Are prices adjusted for splits and bonuses?",
        answer: [
          "Yes, and by default. Splits, bonuses and cash dividends are folded into an adjustment factor, so every factor is computed on a total-return series. The exchange print is stored alongside it and is what the price column and the price filter use, because that is the number you see on your broker's screen.",
          "Rights issues are the exception: pricing them needs the subscription price, which the exchange's free-text notice usually omits. Rather than guess, those actions are left unadjusted and are reported in the pipeline's own audit trail.",
        ],
      },
      {
        id: "pe-empty",
        question: "Why is the P/E column empty?",
        answer: [
          "Because nothing populates it yet. The nightly pipeline has ten steps and none of them fetches fundamentals, so price-to-earnings is null for every instrument and the P/E filter has nothing to filter on.",
          "It is a real gap, it is written down as one, and the column renders as an em dash rather than as a zero.",
        ],
      },
      {
        id: "where-from",
        question: "Where does the data come from?",
        answer: [
          "Daily bars come from Zerodha Kite; corporate actions, index membership and the listings register come from NSE's own published files. Both are licensed for our own use, which is why Baskfy publishes derived analytics and does not expose a raw-bar feed.",
        ],
      },
    ],
  },
  {
    heading: "Using the screener",
    entries: [
      {
        id: "sentinels",
        question: "What do the values 100, 0 and 250 mean in the filters?",
        answer: [
          "They are the off positions. An away-from-high filter set to 100 means “within 100% of the high”, which excludes nothing; a positive-days filter at 0 means “at least 0% of days closed up”, which also excludes nothing; and a circuit filter above 250 permits more circuit days than there are trading days in a year.",
          "The form marks a field visually inactive when it sits at its sentinel, and the group's badge stops counting it, so a collapsed filter group never hides live state.",
        ],
      },
      {
        id: "multi-factor",
        question: "How does combined ranking across two or three factors work?",
        answer: [
          "Every filter except the sort is applied first. The survivors are ranked by factor one, then independently ranked by factor two and factor three. The ranks are summed and the result is sorted ascending on that sum.",
          "It is a Borda count, not a weighted blend, so a name that is 3rd and 40th ends up level with one that is 21st and 22nd.",
        ],
      },
      {
        id: "circuits",
        question: "Are circuit-hit stocks flagged or removed?",
        answer: [
          "Removed. A stock that exceeds the permitted number of circuit days in the window is excluded from the ranking entirely rather than being ranked with a warning on it, because a price series full of limit moves is not measuring the same thing as one that traded.",
        ],
      },
      {
        id: "nulls",
        question: "What happens to a stock with too little history?",
        answer: [
          "A factor with an incomplete window is null, and a null never satisfies a predicate. A stock listed three months ago has no one-year return, so any one-year filter excludes it. It is not treated as a zero and it is not ranked last; it is absent.",
        ],
      },
      {
        id: "export",
        question: "Can I export the results?",
        answer: [
          "Yes, as CSV, on the paid plans. The export carries the full ninety-three-column schema rather than only the columns on screen, and it is rounded from the same stored values the table reads, so a spreadsheet and the page can never disagree.",
        ],
      },
    ],
  },
  {
    heading: "Account and billing",
    entries: [
      {
        id: "whats-gated",
        question: "What do I get for paying?",
        answer: [
          "The screener itself is open. CSV export, custom result columns, historical ranks and backtests are on the paid plans, along with community access.",
          "The pricing page reads the plan list from the billing service, so what it shows is what the server will actually charge and grant.",
        ],
      },
      {
        id: "forever",
        question: "What does the Forever plan mean?",
        answer: [
          "It means the lifetime of the service, not the lifetime of the buyer. That is stated at the point of sale, before payment, because it is the only honest reading of a one-time price on a subscription product.",
        ],
      },
      {
        id: "gst",
        question: "Are the prices inclusive of GST?",
        answer: [
          "Yes. The advertised price is what is charged; the taxable value is back-computed from it and the tax is the remainder, so the invoice total is always the payment to the paisa. Every invoice records the rate it was raised at.",
        ],
      },
      {
        id: "refunds",
        question: "Can I get a refund?",
        answer: [
          "The refund policy page sets out the terms. Read it before buying rather than after.",
        ],
      },
      {
        id: "delete-account",
        question: "Can I delete my account and my data?",
        answer: [
          "Yes. The profile page has an export of everything held about you and a deletion request, both of which the DPDP Act requires. Deletion is a seven-day soft delete you can cancel, after which the account is removed — except where an invoice exists, in which case the account is anonymised instead, because tax law requires the invoice to survive.",
        ],
      },
    ],
  },
  {
    heading: "Limits",
    entries: [
      {
        id: "no-api",
        question: "Is there an API?",
        answer: [
          "Not yet, and it is deliberately switched off rather than merely unbuilt. Market data reaches us under a licence for our own use, and re-publishing it through a third-party API needs a written data-redistribution opinion first.",
        ],
      },
      {
        id: "no-intraday",
        question: "Is there intraday or real-time data?",
        answer: [
          "No. Everything is end-of-day. The pipeline runs after the session closes and publishes one dated snapshot, and the freshness pill in the top bar always says which date you are looking at.",
        ],
      },
      {
        id: "backtest-honesty",
        question: "How trustworthy are the backtests?",
        answer: [
          "They are point-in-time by construction: the engine physically cannot read a row dated after the simulated date, and that is asserted by tests rather than by discipline. Every run carries an assumptions panel naming its costs, its rebalance rule and the periods where index membership had to be reconstructed.",
          "The risk-free rate is currently a flat annual figure defaulting to zero, so every Sharpe and Sortino figure is an excess-over-zero number until a Treasury-bill series exists. That is stated on the page, not buried.",
        ],
      },
    ],
  },
] as const;

export const FAQ_ENTRIES: readonly FaqEntry[] = FAQ_SECTIONS.flatMap((section) => section.entries);
