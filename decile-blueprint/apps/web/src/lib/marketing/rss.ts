import type { PostMeta } from "@/lib/marketing/post-meta";
import { SITE_DESCRIPTION, SITE_NAME, SITE_URL } from "@/lib/site";

/**
 * The blog's RSS 2.0 document — Prompt 18 §2 ("`/blog` with MDX posts and RSS").
 *
 * Hand-written XML rather than a feed library. RSS 2.0 is nine elements; a dependency for nine
 * elements is a dependency to audit, to update, and to justify against docs/02's locked stack.
 *
 * A module rather than code inside the route handler for a mundane reason and a good one: a Next
 * route module may only export the HTTP verbs and a fixed set of config keys, so a helper exported
 * from `route.ts` is a build error — and these two functions are the only part of a feed worth
 * testing, which they cannot be while they are unreachable from Vitest.
 */

/** IST is UTC+05:30 and has no daylight saving, so the offset is a constant. */
const IST_OFFSET = "+0530";

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"] as const;
const MONTHS = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
] as const;

/**
 * `2026-08-12` → `Wed, 12 Aug 2026 00:00:00 +0530`. RSS requires RFC 822, which is not ISO 8601.
 *
 * The weekday is taken from the date read as **UTC midnight**, not from the instant the
 * IST-midnight timestamp denotes. Those are different days: `2026-08-12T00:00:00+05:30` is
 * `2026-08-11T18:30:00Z`, whose UTC weekday is the one before — and a feed whose weekday
 * contradicts its own date is one a reader may refuse. Reading the calendar date as UTC and then
 * stamping the IST offset on the output keeps the two agreeing.
 *
 * A post's date is a plain `YYYY-MM-DD`, anchored at midnight **IST** because that is the timezone
 * publication actually happens in; anchoring at UTC would date a post to the previous day for half
 * the world.
 */
export function rfc822(isoDate: string): string {
  const calendarDay = new Date(`${isoDate}T00:00:00Z`);
  const weekday = WEEKDAYS[calendarDay.getUTCDay()];
  const month = MONTHS[Number(isoDate.slice(5, 7)) - 1];
  return `${weekday}, ${isoDate.slice(8, 10)} ${month} ${isoDate.slice(0, 4)} 00:00:00 ${IST_OFFSET}`;
}

/**
 * The five characters XML reserves.
 *
 * Post titles and summaries are ours, not user input, but an apostrophe and an ampersand are not
 * hypothetical and one unescaped `&` makes the whole document unparseable rather than degrading
 * one item.
 */
export function escapeXml(value: string): string {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&apos;");
}

export function renderFeed(posts: readonly PostMeta[]): string {
  const items = posts
    .map((post) => {
      const url = `${SITE_URL}/blog/${post.slug}`;
      return [
        "    <item>",
        `      <title>${escapeXml(post.title)}</title>`,
        `      <link>${url}</link>`,
        `      <guid isPermaLink="true">${url}</guid>`,
        `      <pubDate>${rfc822(post.date)}</pubDate>`,
        `      <description>${escapeXml(post.summary)}</description>`,
        "    </item>",
      ].join("\n");
    })
    .join("\n");

  return [
    '<?xml version="1.0" encoding="UTF-8"?>',
    '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
    "  <channel>",
    `    <title>${escapeXml(`${SITE_NAME} — blog`)}</title>`,
    `    <link>${SITE_URL}/blog</link>`,
    `    <description>${escapeXml(SITE_DESCRIPTION)}</description>`,
    "    <language>en-in</language>",
    `    <atom:link href="${SITE_URL}/blog/rss.xml" rel="self" type="application/rss+xml"/>`,
    items,
    "  </channel>",
    "</rss>",
    "",
  ].join("\n");
}
