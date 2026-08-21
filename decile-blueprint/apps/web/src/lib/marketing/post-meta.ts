/**
 * The blog's metadata, with **no MDX import in the module graph** — Prompt 18 §2.
 *
 * Split from `posts.ts` for a practical reason. Three consumers need a post's slug, title, date or
 * summary and none of them renders its body: the sitemap, the RSS feed, and two test suites.
 * Vitest and Playwright both transpile with esbuild and neither knows what `.mdx` is, so a module
 * that imports one is unreachable from a test — and the post registry is exactly the thing worth
 * testing, since a renamed file or a duplicated slug is how a blog breaks.
 *
 * `posts.ts` joins this list to the compiled bodies. That join is the only place MDX is imported.
 *
 * ## Dates
 *
 * `date` is the publication date in ISO form and is the ordering key. There is no `updated` field:
 * a post that needs a correction gets a correction *in it*, dated, rather than a silently moved
 * timestamp.
 */
export interface PostMeta {
  slug: string;
  title: string;
  /** One sentence. Used in the index, the `<meta name="description">` and the RSS `<description>`. */
  summary: string;
  /** ISO date, `YYYY-MM-DD`. */
  date: string;
}

export const POST_META: readonly PostMeta[] = [
  {
    slug: "what-a-decile-actually-measures",
    title: "What a decile actually measures",
    summary:
      "Calendar windows rather than bar counts, an adjusted series for the maths and an exchange print for the price column, and why a null is not a zero.",
    date: "2026-08-12",
  },
  {
    slug: "point-in-time-or-it-did-not-happen",
    title: "Point-in-time, or it did not happen",
    summary:
      "The two look-ahead traps in Indian equities, what we do about each of them, and the one we have not solved.",
    date: "2026-07-29",
  },
  {
    slug: "reproducing-a-competitors-export",
    title: "Reproducing 271 rows of somebody else's export",
    summary:
      "What reproduced exactly, what did not, and why agreeing with a competitor validates nothing on its own.",
    date: "2026-07-08",
  },
] as const;

/** Newest first — the order the index and the feed both use. */
export const POST_META_BY_DATE: readonly PostMeta[] = [...POST_META].sort((a, b) =>
  b.date.localeCompare(a.date),
);
