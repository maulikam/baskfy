/** Site-wide constants — docs/14 §"The name" and §"Positioning line". */
export const SITE_NAME = "Baskfy";

/** docs/14: "`baskfy.com` (primary)". */
export const SITE_URL = process.env.NEXT_PUBLIC_SITE_URL ?? "https://baskfy.com";

/**
 * The positioning line.
 *
 * **Superseded docs/14 on 24 Aug 2026** (Maulik). The old line — "Rank every NSE stock by
 * momentum, and know exactly which ones still belong in the top ten percent" — described the
 * screener, which is one of three things this product does and not the one most people arrive
 * for. A visitor read it as "a momentum tool", which is exactly the misread it caused.
 *
 * What the product actually is: one place to run every strategy you hold, whoever authored it —
 * a manager's basket, a rule you wrote yourself, or the long-term holdings you manage by hand —
 * each with its own capital, in your own demat, across whichever brokers you use.
 * `docs/14` §"Positioning line" needs the hand-edit; `docs/DECISIONS-MERGE.md` §UI-4 records why.
 */
export const SITE_TAGLINE = "Every strategy you run, in one place.";

export const SITE_DESCRIPTION =
  "Baskfy is one place for every strategy you run on Indian equities: subscribe to a manager's " +
  "basket, build and test your own rules, or track the holdings you manage by hand — each with " +
  "its own capital, in your own demat. Not investment advice.";
