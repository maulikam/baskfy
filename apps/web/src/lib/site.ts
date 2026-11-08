/** Site-wide constants — docs/14 §"The name" and §"Positioning line". */

export const SITE_NAME = "Baskfy";

/**
 * Resolve a public URL env var. In production the value must be set explicitly — a silent
 * fallback would point staging at the production desk / site (AUDIT 2.9 / 4.6).
 */
export function requiredPublicUrl(
  name: string,
  value: string | undefined,
  devFallback: string,
): string {
  const trimmed = value?.trim();
  if (trimmed) return trimmed.replace(/\/+$/, "");
  if (process.env.NODE_ENV === "production") {
    throw new Error(`${name} must be set in production`);
  }
  return devFallback.replace(/\/+$/, "");
}

/** docs/14: "`baskfy.com` (primary)". */
export const SITE_URL = requiredPublicUrl(
  "NEXT_PUBLIC_SITE_URL",
  process.env.NEXT_PUBLIC_SITE_URL,
  "http://localhost:3000",
);

/** Operator desk console. Staging must set this or links silently go to production. */
export const DESK_CONSOLE_URL = requiredPublicUrl(
  "NEXT_PUBLIC_DESK_URL",
  process.env.NEXT_PUBLIC_DESK_URL,
  "https://desk.modelbasket.in",
);

/**
 * On-demand revalidation shared secret. Required in production so `/api/revalidate` cannot
 * be left open by an empty compare (AUDIT 2.9).
 */
export function assertRevalidateSecretConfigured(): void {
  if (process.env.NODE_ENV !== "production") return;
  if (!process.env.REVALIDATE_SECRET?.trim()) {
    throw new Error("REVALIDATE_SECRET must be set in production");
  }
}

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
