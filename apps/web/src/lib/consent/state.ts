/**
 * The consent record, as it is stored in the browser — Prompt 18 deliverable 5:
 *
 *     "A cookie/consent banner that defaults to essential-only."
 *
 * **Default-deny is the whole design.** Nothing outside `essential` is ever true until the visitor
 * says so, and the absence of a stored choice is read as essential-only rather than as "ask again
 * and meanwhile assume yes". docs/11 §"Compliance & legal (India)" lists the DPDP Act's "consent
 * record" among the obligations; the DPDP Act's consent must be free, specific, informed and
 * unambiguous, which rules out a pre-ticked box and rules out treating "kept browsing" as assent.
 *
 * A plain module with no React in it, so the middleware, the banner and the tests can all read the
 * same rules.
 *
 * ## What each category actually covers today
 *
 * | Category | What sets a cookie | Can it be refused? |
 * |---|---|---|
 * | `essential` | `authjs.session-token`, the CSRF pair, the announcement dismissal, this record | no — the app cannot sign anyone in without it |
 * | `analytics` | **nothing yet** | yes |
 * | `preferences` | the theme choice (`localStorage`, not a cookie) | yes |
 *
 * The `analytics` row is empty on purpose: this product ships no analytics tag, and the banner
 * says so rather than offering a toggle that controls nothing. If one is ever added it is gated
 * on `analytics`, and `src/lib/__tests__/consent.test.ts` is where that promise is asserted.
 */

export const CONSENT_COOKIE = "decile_consent";

/** A year. Long enough not to nag, short enough that consent is renewed rather than assumed. */
export const CONSENT_MAX_AGE_SECONDS = 365 * 24 * 60 * 60;

/** The version of the notice the choice was made against. Bumping it re-asks everyone. */
export const CONSENT_VERSION = 1;

export type ConsentCategory = "essential" | "analytics" | "preferences";

export interface ConsentRecord {
  version: number;
  /** Always true. Present so a stored record is self-describing rather than a set of absences. */
  essential: true;
  analytics: boolean;
  preferences: boolean;
}

/** What a visitor who has chosen nothing is treated as having chosen. */
export const ESSENTIAL_ONLY: ConsentRecord = {
  version: CONSENT_VERSION,
  essential: true,
  analytics: false,
  preferences: false,
};

export const ACCEPT_ALL: ConsentRecord = {
  version: CONSENT_VERSION,
  essential: true,
  analytics: true,
  preferences: true,
};

/**
 * Parse a stored value.
 *
 * Anything unparseable, or written against an older notice, is `null` — which the caller renders
 * as "no choice made yet" and therefore as {@link ESSENTIAL_ONLY}. A corrupt cookie must never
 * decay into a *permissive* record.
 */
export function parseConsent(raw: string | null | undefined): ConsentRecord | null {
  if (!raw) return null;
  let decoded: unknown;
  try {
    decoded = JSON.parse(decodeURIComponent(raw));
  } catch {
    return null;
  }
  if (typeof decoded !== "object" || decoded === null) return null;
  const candidate = decoded as Record<string, unknown>;
  if (candidate.version !== CONSENT_VERSION) return null;
  return {
    version: CONSENT_VERSION,
    essential: true,
    analytics: candidate.analytics === true,
    preferences: candidate.preferences === true,
  };
}

export function serialiseConsent(record: ConsentRecord): string {
  return encodeURIComponent(JSON.stringify(record));
}

/** The effective record for a visitor: their stored choice, or essential-only. */
export function effectiveConsent(raw: string | null | undefined): ConsentRecord {
  return parseConsent(raw) ?? ESSENTIAL_ONLY;
}

export function isAllowed(raw: string | null | undefined, category: ConsentCategory): boolean {
  const record = effectiveConsent(raw);
  return category === "essential" ? true : record[category];
}
