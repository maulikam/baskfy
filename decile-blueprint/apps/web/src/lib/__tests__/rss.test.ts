import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";

import { POST_META as POSTS } from "@/lib/marketing/post-meta";

/**
 * The origin the feed is rendered against.
 *
 * It is configuration, not a constant: since `2e4437e` `lib/site.ts` reads
 * `NEXT_PUBLIC_SITE_URL` and *refuses to guess* in production, falling back to
 * `http://localhost:3000` only outside it — which is what a bare `vitest run` is. So this file
 * configures the origin the way the production build does (`tools/deploy/push-images.sh` passes
 * it as a build arg) and asserts the literal permalink, rather than reading `SITE_URL` back out
 * of the module under test and agreeing with it about anything. A guid is a reader's permanent
 * key for an item; one that says `localhost` files the post under an address nobody can reach.
 *
 * `lib/site.ts` evaluates at import, so the module is loaded after the stub, the same way
 * `hsts-preload.test.ts` reloads `next.config`.
 */
const ORIGIN = "https://baskfy.com";

let escapeXml: (value: string) => string;
let rfc822: (isoDate: string) => string;
let feed: string;

beforeAll(async () => {
  vi.stubEnv("NEXT_PUBLIC_SITE_URL", ORIGIN);
  vi.resetModules();
  const rss = await import("@/lib/marketing/rss");
  ({ escapeXml, rfc822 } = rss);
  feed = rss.renderFeed(POSTS);
});

afterAll(() => {
  vi.resetModules();
  vi.unstubAllEnvs();
});

/**
 * RSS 2.0 requires RFC 822 dates, which are **not** ISO 8601. Getting them wrong does not fail a
 * build, does not fail a render, and does not fail Lighthouse — a reader simply shows the wrong
 * date, or refuses the item.
 *
 * The specific bug this exists to prevent was live for one build: the weekday was read from
 * `new Date("2026-08-12T00:00:00+05:30").getUTCDay()`, which is the weekday of
 * `2026-08-11T18:30:00Z` — the day before. Every `pubDate` in the feed named a weekday that did
 * not match its own date.
 */
describe("the feed's dates are RFC 822", () => {
  it.each([
    ["2026-08-12", "Wed, 12 Aug 2026 00:00:00 +0530"],
    ["2026-07-29", "Wed, 29 Jul 2026 00:00:00 +0530"],
    ["2026-07-08", "Wed, 08 Jul 2026 00:00:00 +0530"],
    ["2026-01-01", "Thu, 01 Jan 2026 00:00:00 +0530"],
    ["2026-12-31", "Thu, 31 Dec 2026 00:00:00 +0530"],
    // A leap day, and a date whose UTC and IST calendar days differ.
    ["2028-02-29", "Tue, 29 Feb 2028 00:00:00 +0530"],
  ])("renders %s as %s", (iso, expected) => {
    expect(rfc822(iso)).toBe(expected);
  });

  it("names the same weekday the calendar does, for every published post", () => {
    for (const post of POSTS) {
      const expected = new Date(`${post.date}T00:00:00Z`).toUTCString().slice(0, 3);
      expect(rfc822(post.date).slice(0, 3), post.date).toBe(expected);
    }
  });

  it("keeps the day of the month it was given", () => {
    for (const post of POSTS) {
      expect(rfc822(post.date)).toContain(post.date.slice(8, 10));
      expect(rfc822(post.date)).toContain(post.date.slice(0, 4));
    }
  });
});

describe("the feed document", () => {
  it("declares itself as RSS 2.0 with an XML prolog", () => {
    expect(feed.startsWith('<?xml version="1.0" encoding="UTF-8"?>')).toBe(true);
    expect(feed).toContain('<rss version="2.0"');
  });

  it("has one item per post, each with a permalink guid at the configured origin", () => {
    expect(feed.match(/<item>/g)?.length).toBe(POSTS.length);
    for (const post of POSTS) {
      expect(feed).toContain(`<guid isPermaLink="true">${ORIGIN}/blog/${post.slug}</guid>`);
    }
  });

  it("puts no development origin in a document a reader subscribes to", () => {
    expect(feed).not.toContain("localhost");
  });

  it("carries the self-referencing atom link readers use to resubscribe", () => {
    expect(feed).toContain('rel="self" type="application/rss+xml"');
  });

  it("escapes the five characters XML reserves", () => {
    expect(escapeXml(`a & b < c > d " e ' f`)).toBe(
      "a &amp; b &lt; c &gt; d &quot; e &apos; f",
    );
  });

  it("leaves no raw ampersand in the document, which would make it unparseable", () => {
    /* Every `&` that survives must be the start of an entity. */
    expect(feed.match(/&(?!(amp|lt|gt|quot|apos|#\d+);)/g)).toBeNull();
  });
});
