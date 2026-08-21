/**
 * The public route table — docs/01 §1's Content and "Legal / marketing" rows, plus docs/08
 * §Routes' "`/pricing`, `/faq`, `/about`, `/blog/*`, legal | SSG".
 *
 * One list, used by four things that must not drift apart: the marketing footer, the sitemap, the
 * `robots.ts` allow set, and `src/app/__tests__/marketing-routes.test.ts`. A legal page that
 * exists but is in no sitemap and no footer is a page nobody will ever read.
 */

import type { Route } from "next";

export interface PublicRoute {
  href: Route;
  label: string;
  /** Sitemap priority. The landing page is 1; content is above legal, which nobody searches for. */
  priority: number;
}

/** What the product is and what it costs. */
export const PRODUCT_ROUTES: readonly PublicRoute[] = [
  { href: "/", label: "Home", priority: 1 },
  { href: "/pricing", label: "Pricing", priority: 0.9 },
  { href: "/dashboard", label: "Indices dashboard", priority: 0.8 },
  { href: "/market-health", label: "Market Pulse", priority: 0.8 },
  { href: "/listings", label: "Listings", priority: 0.6 },
] as const;

/** docs/01 §1: "`/faq`, `/support`, `/blog`, `/ama-recording`, `/inspire` | Content". */
export const CONTENT_ROUTES: readonly PublicRoute[] = [
  { href: "/faq", label: "FAQ", priority: 0.7 },
  { href: "/blog", label: "Blog", priority: 0.7 },
  { href: "/about", label: "About", priority: 0.5 },
  { href: "/support", label: "Support", priority: 0.5 },
  { href: "/december-2026-update", label: "December 2026 update", priority: 0.5 },
] as const;

/**
 * docs/11 §Compliance: "Terms & Conditions, Privacy Policy, Refund Policy pages before taking a
 * single payment." `/disclaimer` is the fourth Prompt 18 asks for and docs/01 §1 does not list;
 * it is where the sentence the `<Disclaimer/>` component carries is set out at length.
 */
export const LEGAL_ROUTES: readonly PublicRoute[] = [
  { href: "/terms-conditions", label: "Terms & Conditions", priority: 0.3 },
  { href: "/privacy-policy", label: "Privacy Policy", priority: 0.3 },
  { href: "/refund-policy", label: "Refund Policy", priority: 0.3 },
  { href: "/disclaimer", label: "Disclaimer", priority: 0.3 },
] as const;

export const PUBLIC_ROUTES: readonly PublicRoute[] = [
  ...PRODUCT_ROUTES,
  ...CONTENT_ROUTES,
  ...LEGAL_ROUTES,
] as const;

/**
 * Routes that are statically generated and carry no session — the set `src/middleware.ts` serves
 * the nonce-free content policy to, and the set Prompt 18's first acceptance criterion is about.
 *
 * `/pricing`, `/dashboard`, `/market-health` and `/listings` are **not** here: each reads live data
 * or the session and renders dynamically (`docs/DECISIONS.md` §18.3).
 */
export const STATIC_PUBLIC_PREFIXES: readonly string[] = [
  "/faq",
  "/about",
  "/support",
  "/blog",
  "/december-2026-update",
  "/terms-conditions",
  "/privacy-policy",
  "/refund-policy",
  "/disclaimer",
] as const;

/** Whether a path is served the static content policy rather than the per-request nonce policy. */
export function isStaticPublicPath(pathname: string): boolean {
  if (pathname === "/") return true;
  return STATIC_PUBLIC_PREFIXES.some(
    (prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`),
  );
}
