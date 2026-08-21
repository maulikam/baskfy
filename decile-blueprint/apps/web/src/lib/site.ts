/** Site-wide constants — docs/14 §"The name" and §"Positioning line". */
export const SITE_NAME = "Decile";

/** docs/14: "`baskfy.com` (primary)". */
export const SITE_URL = process.env.NEXT_PUBLIC_SITE_URL ?? "https://baskfy.com";

/** docs/14 §"Positioning line", verbatim. */
export const SITE_TAGLINE =
  "Rank every NSE stock by momentum, and know exactly which ones still belong in the top ten percent.";

export const SITE_DESCRIPTION =
  "Decile ranks every NSE-listed stock by momentum across 64 factors, with point-in-time index " +
  "membership and published formulas. Not investment advice.";
