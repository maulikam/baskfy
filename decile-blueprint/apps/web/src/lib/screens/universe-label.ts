/**
 * Human labels for the index-universe chip (§1.1).
 *
 * Two separate things were wrong before this module existed. The chip read the raw slug
 * `nifty-total-market` until `/meta/universes` resolved — the universe list is its own fetch, so
 * that is the *normal* first paint rather than an edge case — and once it resolved the chip read
 * the API's `NIFTY TOTAL MARKET`, because the seed stores index names the way NSE prints them.
 *
 * The rule §1.1 asks for is one sentence: keep the brand and the acronyms upper-case, title-case
 * the descriptive words, never re-case a number, and leave a name that was already written for
 * humans (`All NSE Listed Stocks`) exactly as it is. Title-casing is idempotent on sentence case,
 * so "leave it alone" needs no special case — only the acronyms do, and they are an explicit set
 * here rather than a regex repeated at each call site.
 *
 * Pure: no React, no fetch, no clock. `universe-label.test.ts` drives every one of the fourteen
 * names `/meta/universes` actually publishes.
 */

/**
 * Words that survive upper-case. Extend this set when a universe arrives carrying a new one —
 * never widen a regex to cover it. A trailing plural is handled here, so `ETF` also covers
 * `ETFs`.
 */
export const UNIVERSE_ACRONYMS: ReadonlySet<string> = new Set(["NIFTY", "NSE", "ETF", "FNO"]);

/** What the chip says when it has neither a usable name nor a usable slug to work from. */
export const UNIVERSE_LABEL_FALLBACK = "Index universe";

/**
 * The two slugs whose published name cannot be recovered from the slug itself.
 *
 * Consulted only while `/meta/universes` is in flight; the server's own name wins the instant it
 * lands. They exist so the loading label is the *same string* as the resolved label — a guess
 * that rewrites itself under the reader is worse than no guess at all — and the test pins all
 * fourteen slug-derived labels against all fourteen name-derived ones, so this cannot quietly
 * drift back into a flicker. Same duplicate-with-a-test-as-the-link trade as
 * `lib/market/universes.ts`.
 */
const SLUG_LABEL_OVERRIDES: Readonly<Record<string, string>> = {
  "nifty-allcap": "All NSE Listed Stocks",
  etf: "All NSE Listed ETFs",
};

/** Letters and digits, one run at a time, so punctuation between them is carried through. */
const ALPHANUMERIC_RUN = /[\p{L}\p{N}]+/gu;
const HAS_DIGIT = /\p{N}/u;
const SLUG_SEPARATORS = /[-_\s]+/;
const WHITESPACE_RUN = /\s+/g;

/** The stored form of an acronym, or `null` when the run is an ordinary word. */
function canonicalAcronym(run: string): string | null {
  const upper = run.toUpperCase();
  if (UNIVERSE_ACRONYMS.has(upper)) return upper;
  if (upper.length > 1 && upper.endsWith("S")) {
    const singular = upper.slice(0, -1);
    if (UNIVERSE_ACRONYMS.has(singular)) return `${singular}s`;
  }
  return null;
}

function humaniseRun(run: string): string {
  const acronym = canonicalAcronym(run);
  if (acronym !== null) return acronym;
  /* An index's number is part of its name: "50", "250", "400". Upper-case is the only safe move
     for letters glued to one, since "12M" is a window and "12m" is a typo. */
  if (HAS_DIGIT.test(run)) return run.toUpperCase();
  return run.charAt(0).toUpperCase() + run.slice(1).toLowerCase();
}

function humanise(text: string): string {
  return text.replace(ALPHANUMERIC_RUN, humaniseRun);
}

function normaliseSlug(slug: string): string {
  return slug.trim().toLowerCase();
}

/** The label a name yields, or `""` when the name says nothing. */
function labelFromName(name: string): string {
  const collapsed = name.trim().replace(WHITESPACE_RUN, " ");
  return collapsed === "" ? "" : humanise(collapsed);
}

/** The label a slug yields, or `""` when the slug says nothing. */
function labelFromSlug(slug: string): string {
  const key = normaliseSlug(slug);
  if (key === "") return "";

  const override = SLUG_LABEL_OVERRIDES[key];
  if (override !== undefined) return override;

  const words = key.split(SLUG_SEPARATORS).filter((word) => word !== "");
  if (words.length === 0) return "";
  return humanise(words.join(" "));
}

/**
 * `NIFTY TOTAL MARKET` → `NIFTY Total Market`, `NIFTY 50` → `NIFTY 50`, and
 * `All NSE Listed Stocks` → `All NSE Listed Stocks`.
 */
export function universeLabelFromName(name: string): string {
  return labelFromName(name) || UNIVERSE_LABEL_FALLBACK;
}

/**
 * The label to show before `/meta/universes` has answered: `nifty-total-market` →
 * `NIFTY Total Market`. Never returns anything slug-shaped.
 */
export function universeLabelFromSlug(slug: string): string {
  return labelFromSlug(slug) || UNIVERSE_LABEL_FALLBACK;
}

/** The shape this module needs from a universe; `UniverseOut` satisfies it. */
export interface UniverseLike {
  readonly slug: string;
  readonly name: string;
}

/**
 * The chip's label at any moment of the page's life: the published name once it is known, the
 * slug-derived reading of it before that, and a fixed fallback if a caller ever manages to hold
 * neither.
 */
export function universeChipLabel(slug: string, universes: readonly UniverseLike[]): string {
  const key = normaliseSlug(slug);
  const match = key === "" ? undefined : universes.find((u) => normaliseSlug(u.slug) === key);
  const published = match ? labelFromName(match.name) : "";
  return published || labelFromSlug(slug) || UNIVERSE_LABEL_FALLBACK;
}
