import { describe, expect, it } from "vitest";

import { isGatedPath, isPublicPath } from "@/lib/auth/public-routes";

/**
 * The gate's spec, not its implementation.
 *
 * The property that matters is the *default*: a path nobody thought about must come back gated.
 * The old middleware failed exactly here — it enumerated what to protect, so every route added
 * after the list was written was public by accident. The first block below is therefore the
 * important one, and it is written as "here is a route this file has never heard of".
 */
describe("the gate is closed by default", () => {
  it.each([
    "/build",
    "/build/exmpl0000001",
    "/explore",
    "/create",
    "/holdings",
    "/watchlist",
    "/me",
    "/me/portfolios",
    "/me/investments/abc/orders",
    "/baskets/featured",
    "/basket/nifty-momentum",
    "/screens",
    "/screens/exmpl0000001/columns",
    "/instruments/CUPID",
    "/market/today",
    "/market-health",
    "/dashboard",
    "/listings",
    "/portfolios",
    "/backtests",
    "/profile",
    "/change-password",
    "/invoices",
    "/admin",
    "/admin/users",
    "/api-keys",
    "/alerts",
    "/kitchen-sink",
    "/tradebook",
    "/reconcile",
  ])("%s requires a session", (path) => {
    expect(isPublicPath(path)).toBe(false);
    expect(isGatedPath(path)).toBe(true);
  });

  it("gates a route that does not exist yet, which is the whole point", () => {
    expect(isGatedPath("/some-surface-nobody-has-built")).toBe(true);
    expect(isGatedPath("/build/2027/rebalance/preview")).toBe(true);
  });
});

describe("what stays public, and why each one has to", () => {
  it.each([
    ["/", "the landing page"],
    ["/pricing", "what it costs, read before deciding to sign up"],
    ["/faq", "content"],
    ["/about", "content"],
    ["/support", "content"],
    ["/blog", "content"],
    ["/blog/what-a-decile-actually-measures", "a post, not only the index"],
    ["/december-2026-update", "the announcement"],
    ["/terms-conditions", "docs/11 §Compliance"],
    ["/privacy-policy", "docs/11 §Compliance"],
    ["/refund-policy", "docs/11 §Compliance"],
    ["/disclaimer", "docs/11 §Compliance"],
    ["/login", "the way in"],
    ["/register", "the way in"],
    ["/forgot-password", "the way in"],
    ["/reset-password", "followed from an email, by definition with no session"],
    ["/verify-email", "followed from an email, by definition with no session"],
    ["/logout", "the way out"],
    ["/api/auth/session", "Auth.js's own endpoints"],
    ["/api/auth/callback/password", "Auth.js's own endpoints"],
    ["/alerts/unsubscribe", "one-click unsubscribe cannot ask for a sign-in first"],
    ["/api/revalidate", "the pipeline webhook, guarded by its own shared secret"],
    ["/opengraph-image", "or every shared link renders a blank card"],
    ["/robots.txt", "crawler metadata"],
    ["/sitemap.xml", "crawler metadata"],
  ])("%s is public — %s", (path) => {
    expect(isPublicPath(path)).toBe(true);
  });
});

describe("files served verbatim out of public/ are not routes", () => {
  /**
   * The regression this pins: the first cut gated `/brand/logo.svg`, so the marketing pages'
   * artwork was redirected to `/login` and the login page's own `img-src 'self'` then refused the
   * redirect. An access control had eaten the logo.
   */
  it.each([
    "/brand/logo.svg",
    "/brand/wordmark-dark.png",
    "/images/hero-stepwell.webp",
    "/fonts/something.woff2",
    "/manifest.webmanifest",
    "/blog/rss.xml",
  ])("%s is served, not gated", (path) => {
    expect(isPublicPath(path)).toBe(true);
  });

  it("matches on a known extension, not on any dot — a symbol may contain one", () => {
    expect(isPublicPath("/instruments/SOME.THING")).toBe(false);
    expect(isPublicPath("/build/a.screen.of.mine")).toBe(false);
  });
});

describe("prefix matching does not leak", () => {
  /**
   * `startsWith` on a bare prefix is how a gate accidentally opens: `/login` must not make
   * `/logins-report` public, and `/alerts/unsubscribe` must not make the whole `/alerts` surface
   * public. The boundary is a path separator or the end of the string, and nothing else.
   */
  it.each([
    "/pricing-internal",
    "/blogging-platform",
    "/support-tickets",
    "/logins",
    "/registered-users",
    "/api/authorisations",
    "/alerts",
    "/alerts/settings",
    "/alerts/unsubscribers",
  ])("%s is gated even though it shares a prefix with a public path", (path) => {
    expect(isPublicPath(path)).toBe(false);
  });

  it("keeps the children of a public prefix public", () => {
    expect(isPublicPath("/alerts/unsubscribe/done")).toBe(true);
    expect(isPublicPath("/blog/rss.xml")).toBe(true);
  });
});
