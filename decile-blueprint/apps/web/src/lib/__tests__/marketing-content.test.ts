import { readdirSync, readFileSync, existsSync } from "node:fs";
import { join, resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { FAQ_ENTRIES, FAQ_SECTIONS } from "@/lib/marketing/faq";
import { POST_META as POSTS, POST_META_BY_DATE as POSTS_BY_DATE } from "@/lib/marketing/post-meta";
import {
  CONTENT_ROUTES,
  LEGAL_ROUTES,
  PRODUCT_ROUTES,
  PUBLIC_ROUTES,
  isStaticPublicPath,
} from "@/lib/marketing/routes";

/**
 * The content surfaces Prompt 18 delivers, checked for the things that break silently.
 *
 * None of these is a rendering test — the pages are server components over static data, and a
 * render test of one would assert that React works. What can actually go wrong here is
 * *bookkeeping*: a route in the footer with no page behind it, a post whose MDX file was renamed,
 * a duplicate anchor that makes two FAQ answers unlinkable, a marketing route that quietly stopped
 * being statically generated.
 */
const APP_DIR = resolve(process.cwd(), "src", "app", "(marketing)");
const BLOG_CONTENT = resolve(process.cwd(), "src", "content", "blog");

describe("every public route has a page behind it", () => {
  it.each(PUBLIC_ROUTES.map((route) => route.href))("%s resolves to a route module", (href) => {
    if (href === "/") {
      expect(existsSync(join(APP_DIR, "page.tsx"))).toBe(true);
      return;
    }
    const marketing = join(APP_DIR, href.slice(1), "page.tsx");
    const app = resolve(process.cwd(), "src", "app", "(app)", href.slice(1), "page.tsx");
    expect(existsSync(marketing) || existsSync(app), `${href} has no page.tsx`).toBe(true);
  });

  it("lists each route exactly once", () => {
    const hrefs = PUBLIC_ROUTES.map((route) => route.href);
    expect(new Set(hrefs).size).toBe(hrefs.length);
  });

  it("puts the landing page first and at priority 1", () => {
    expect(PUBLIC_ROUTES[0]?.href).toBe("/");
    expect(PUBLIC_ROUTES[0]?.priority).toBe(1);
  });

  it("keeps every priority inside the sitemap protocol's range", () => {
    for (const route of PUBLIC_ROUTES) {
      expect(route.priority).toBeGreaterThan(0);
      expect(route.priority).toBeLessThanOrEqual(1);
    }
  });

  it("carries the four legal documents docs/11 and Prompt 18 name", () => {
    expect(LEGAL_ROUTES.map((route) => route.href)).toEqual([
      "/terms-conditions",
      "/privacy-policy",
      "/refund-policy",
      "/disclaimer",
    ]);
  });
});

describe("the static-policy predicate matches what is actually static", () => {
  /**
   * `isStaticPublicPath` decides which routes get the nonce-free CSP in `src/middleware.ts`. If it
   * said yes to a route that renders a session, that route would lose its nonce policy; if it said
   * no to a statically generated one, that page's own inline bootstrap would be blocked.
   */
  it.each(CONTENT_ROUTES.concat(LEGAL_ROUTES).map((route) => route.href))(
    "%s is served the static policy",
    (href) => {
      expect(isStaticPublicPath(href)).toBe(true);
    },
  );

  it("includes the landing page", () => {
    expect(isStaticPublicPath("/")).toBe(true);
  });

  it.each(["/dashboard", "/market-health", "/listings", "/pricing"])(
    "%s is not, because it reads live data or the session",
    (href) => {
      expect(isStaticPublicPath(href)).toBe(false);
    },
  );

  it.each(["/screens", "/profile", "/admin", "/api/auth/session", "/login"])(
    "%s keeps the nonce policy",
    (href) => {
      expect(isStaticPublicPath(href)).toBe(false);
    },
  );

  it("covers a blog post, not only the index", () => {
    expect(isStaticPublicPath("/blog/what-a-decile-actually-measures")).toBe(true);
  });

  it("does not match a route that merely starts with the same letters", () => {
    expect(isStaticPublicPath("/blogging-platform")).toBe(false);
    expect(isStaticPublicPath("/support-tickets")).toBe(false);
  });

  it("keeps every product route out of it except the landing page", () => {
    for (const route of PRODUCT_ROUTES.filter((candidate) => candidate.href !== "/")) {
      expect(isStaticPublicPath(route.href)).toBe(false);
    }
  });
});

describe("the blog", () => {
  it("has an MDX file for every registered post", () => {
    for (const post of POSTS) {
      expect(existsSync(join(BLOG_CONTENT, `${post.slug}.mdx`)), post.slug).toBe(true);
    }
  });

  it("registers every MDX file that is a post", () => {
    /* `december-2026-update.mdx` lives here too but is an *announcement*, served by its own route.
       It is excluded by name rather than by directory so that adding a post and forgetting to
       register it still fails. */
    const announcements = new Set(["december-2026-update.mdx"]);
    const onDisk = readdirSync(BLOG_CONTENT)
      .filter((name) => name.endsWith(".mdx") && !announcements.has(name))
      .map((name) => name.replace(/\.mdx$/, ""))
      .sort();
    expect(onDisk).toEqual(POSTS.map((post) => post.slug).sort());
  });

  it("has unique slugs", () => {
    const slugs = POSTS.map((post) => post.slug);
    expect(new Set(slugs).size).toBe(slugs.length);
  });

  it("dates every post as a plain ISO day", () => {
    for (const post of POSTS) expect(post.date).toMatch(/^\d{4}-\d{2}-\d{2}$/);
  });

  it("orders the index newest first", () => {
    const dates = POSTS_BY_DATE.map((post) => post.date);
    expect([...dates].sort().reverse()).toEqual(dates);
  });

  it("gives every post a summary that would read as a feed description", () => {
    for (const post of POSTS) {
      expect(post.summary.length, post.slug).toBeGreaterThan(40);
      expect(post.summary.length, post.slug).toBeLessThan(320);
    }
  });

  it("has a body in every MDX file", () => {
    for (const post of POSTS) {
      const source = readFileSync(join(BLOG_CONTENT, `${post.slug}.mdx`), "utf-8");
      expect(source.trim().length, post.slug).toBeGreaterThan(500);
    }
  });
});

describe("the FAQ", () => {
  it("has a unique anchor for every question, so an answer can be linked to", () => {
    const ids = FAQ_ENTRIES.map((entry) => entry.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("uses anchors that are valid URL fragments", () => {
    for (const entry of FAQ_ENTRIES) expect(entry.id).toMatch(/^[a-z0-9-]+$/);
  });

  it("phrases every question as a question", () => {
    for (const entry of FAQ_ENTRIES) expect(entry.question.endsWith("?")).toBe(true);
  });

  it("answers every question", () => {
    for (const entry of FAQ_ENTRIES) {
      expect(entry.answer.length, entry.id).toBeGreaterThan(0);
      for (const paragraph of entry.answer) expect(paragraph.length).toBeGreaterThan(40);
    }
  });

  it("has a section heading for each group and no empty group", () => {
    for (const section of FAQ_SECTIONS) {
      expect(section.heading.length).toBeGreaterThan(0);
      expect(section.entries.length).toBeGreaterThan(0);
    }
  });

  it("states the three known gaps, because a visitor should not have to discover them", () => {
    const text = FAQ_ENTRIES.flatMap((entry) => entry.answer).join(" ").toLowerCase();
    expect(text).toContain("price-to-earnings");
    expect(text).toContain("1 november 2024");
    expect(text).toContain("risk-free rate");
  });
});
