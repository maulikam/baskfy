import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { expect, test } from "@playwright/test";

import { CONTENT_ROUTES, LEGAL_ROUTES } from "../src/lib/marketing/routes";
import { POST_META } from "../src/lib/marketing/post-meta";

/**
 * The first half of Prompt 18's first acceptance criterion — "**All pages are statically
 * generated** and score ≥ 95 on Lighthouse performance and SEO."
 *
 * Read out of `.next/prerender-manifest.json`, which `next build` writes and which lists exactly
 * the routes Next prerendered to HTML. That is the build's own record rather than an inference:
 * a page that quietly became dynamic — someone adds a `cookies()` read to a shared component —
 * disappears from the manifest, and this fails.
 *
 * Not a browser test at all, but it belongs in this project: the Playwright config is what runs
 * `next build`, and asserting the manifest before a Vitest run that never built anything would be
 * asserting a stale file.
 *
 * ## Which pages the criterion is read as covering
 *
 * The content and legal routes, the landing page, the blog and its feed. **Not** `/pricing`,
 * `/dashboard`, `/market-health` or `/listings`: each reads live data or the session, docs/08
 * §Routes marks the last three "RSC" rather than SSG, and `/pricing` renders the caller's current
 * plan. `docs/DECISIONS.md` §18.3 records the reading.
 */
/**
 * **`.next-e2e`, not `.next`** — and this file asserted nothing at all until 24 Aug 2026 because
 * of it. `playwright.config.ts` builds into a directory of its own (`BASKFY_WEB_DIST_DIR`) so a
 * suite run cannot overwrite a running dev server's chunks; that variable is set on the *web
 * server* process, not on the test process, so this read kept hitting the dev server's `.next`,
 * where `prerender-manifest.json` lists zero routes. Every assertion below passed vacuously or
 * failed for the wrong reason, and "the routes that read live data are honestly not in it" passed
 * because an empty set contains nothing.
 */
const DIST_DIR = process.env.BASKFY_WEB_DIST_DIR ?? ".next-e2e";
const MANIFEST = resolve(process.cwd(), DIST_DIR, "prerender-manifest.json");

interface PrerenderManifest {
  routes: Record<string, unknown>;
  dynamicRoutes: Record<string, unknown>;
}

function prerendered(): Set<string> {
  const manifest = JSON.parse(readFileSync(MANIFEST, "utf-8")) as PrerenderManifest;
  return new Set(Object.keys(manifest.routes));
}

const EXPECTED = [
  "/",
  ...CONTENT_ROUTES.map((route) => route.href),
  ...LEGAL_ROUTES.map((route) => route.href),
  "/blog/rss.xml",
  ...POST_META.map((post) => `/blog/${post.slug}`),
];

test.describe("the public content surface is prerendered", () => {
  test("the build wrote a prerender manifest", () => {
    expect(prerendered().size).toBeGreaterThan(0);
  });

  for (const route of EXPECTED) {
    test(`${route} is statically generated`, () => {
      expect([...prerendered()].sort().join("\n")).toContain(route);
      expect(prerendered().has(route), `${route} is not in the prerender manifest`).toBe(true);
    });
  }

  test("the routes that read live data are honestly not in it", () => {
    /* Asserted as an expectation, not tolerated as an absence: if `/pricing` ever becomes static
       it will be because someone removed the per-caller plan state, and that should be a
       deliberate change rather than a silent one. */
    const routes = prerendered();
    for (const dynamic of ["/pricing", "/dashboard", "/market-health", "/listings", "/screens", "/build"]) {
      expect(routes.has(dynamic), `${dynamic} unexpectedly became static`).toBe(false);
    }
  });
});
