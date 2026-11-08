import { STATIC_PUBLIC_PREFIXES } from "@/lib/marketing/routes";

/**
 * The public surface — every path that renders without a session. **Everything else is gated.**
 *
 * This inverts what the gate used to be. `src/middleware.ts` carried a `GATED_PREFIXES`
 * allow-by-default list (`/profile`, `/portfolios`, `/backtests`, `/admin`, …), which meant a new
 * route under `(app)` was public until somebody remembered to add it — and by August 2026 that
 * "somebody remembered" had not happened for `/build`, `/explore`, `/create`, `/me/*`,
 * `/holdings`, `/watchlist`, `/discover/*`, `/basket/*`, `/instruments/*` or `/screens/*`. A gate
 * whose default is *open* fails silently, and the failure is invisible in review because the
 * diff that adds a page never mentions the middleware.
 *
 * So the list here is the opposite: a closed default, and an explicit, argued exception for each
 * public path. Adding a page now costs nothing; making one public is a deliberate edit to this
 * file. `src/lib/auth/__tests__/public-routes.test.ts` pins the shape.
 *
 * Note this is a *route* list, not the enforcement. Three layers stand behind it, in order of how
 * much they can be trusted: the middleware redirect (fast, cookie-presence only), the `(app)`
 * layout's `auth()` check (validates the session token server-side), and `services/api`, which
 * authorises every read against the bearer token regardless of what the browser was allowed to
 * render (`docs/12a` §11).
 */

/** Paths that are public and have no children. */
const PUBLIC_EXACT: ReadonlySet<string> = new Set([
  "/",
  /* Route-level metadata files. Next serves these from `src/app/`; a login redirect on the
     Open Graph image means every shared link renders with a blank card. */
  "/opengraph-image",
  "/icon.png",
  "/apple-icon.png",
  "/favicon.ico",
  "/robots.txt",
  "/sitemap.xml",
]);

/** Paths that are public together with everything beneath them. */
const PUBLIC_PREFIXES: readonly string[] = [
  /* The way in, and since Google sign-in replaced registration it is the only one
     (`docs/DECISIONS-MERGE.md` M46). `/register`, `/forgot-password`, `/reset-password` and
     `/verify-email` were here too; all four pages are gone. Gating this would be a redirect
     loop. */
  "/login",
  /* The way out. `/logout` clears the cookie; requiring a session to reach it is harmless but
     confusing when a stale cookie is exactly what somebody is trying to get rid of. */
  "/logout",
  /* Auth.js's own endpoints — the credential post, the CSRF token, the session probe the
     client-side sentinel reads. */
  "/api/auth",
  /* Marketing, content and the four legal documents. docs/11 §Compliance requires the legal
     pages to be readable by a regulator or a payment provider who will never have an account. */
  ...STATIC_PUBLIC_PREFIXES,
  /* What the product costs, readable before deciding to sign up. Lives under `(app)` because it
     renders in the app shell, which is a layout decision, not an access decision. */
  "/pricing",
  /* One-click unsubscribe, from the footer of every alert email. The page's own docstring is
     explicit that it acts with no session: "a page that asked the recipient to sign in first
     would be useless to exactly the people most likely to use it". */
  "/alerts/unsubscribe",
  /* The nightly cache-purge webhook, guarded by its own shared secret compared in constant time
     (`src/app/api/revalidate/route.ts`). It is called by the data pipeline, which has no cookie. */
  "/api/revalidate",
];

/**
 * The files Next serves verbatim out of `public/` — `/brand/logo.svg`, `/images/hero-stepwell.webp`
 * and whatever joins them.
 *
 * They are **not** routes and must never meet the gate. The first cut of this file left them out,
 * and the browser suite caught it in the most legible way possible: a signed-out visitor's login
 * page reported `Loading the image 'https://…/login?next=%2Fbrand%2Flogo.svg' violates … img-src`.
 * The gate had redirected the logo to the login page, and the login page's own CSP then refused
 * the redirect — so the marketing pages lost their art to a security control that was never meant
 * to see them. `_next/static` and `_next/image` were already excluded at the matcher; `public/`
 * never was, because nothing had ever needed it to be.
 *
 * Matched by **extension**, from a closed list, rather than by "the last segment contains a dot".
 * That distinction is the whole point: `/instruments/SOME.THING` is a route, and a dot rule would
 * quietly un-gate every symbol that has one.
 */
const ASSET_EXTENSION =
  /\.(?:svg|png|jpe?g|gif|webp|avif|ico|bmp|woff2?|ttf|otf|eot|css|js|mjs|map|txt|xml|json|webmanifest|mp4|webm|ogg|mp3|wav|pdf|csv)$/i;

/** Whether `pathname` renders without a session. Everything not listed here is gated. */
export function isPublicPath(pathname: string): boolean {
  if (ASSET_EXTENSION.test(pathname)) return true;
  if (PUBLIC_EXACT.has(pathname)) return true;
  return PUBLIC_PREFIXES.some(
    (prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`),
  );
}

/** The complement, spelled out so call sites read as the thing they are deciding. */
export function isGatedPath(pathname: string): boolean {
  return !isPublicPath(pathname);
}

export { PUBLIC_EXACT, PUBLIC_PREFIXES };
