import { NextResponse, type NextRequest } from "next/server";

import { isPublicPath } from "@/lib/auth/public-routes";
import { isStaticPublicPath } from "@/lib/marketing/routes";

/**
 * Two jobs, both from docs/11 §Security (Prompt 12 deliverables 2 and 4).
 *
 * **The gate, and it is now closed by default.** Every path that is not on the public list in
 * `@/lib/auth/public-routes` is redirected to `/login?next=…` when the request carries no session
 * cookie. It used to be the other way round — an enumerated list of gated prefixes, everything
 * else public — and that list had fallen a year behind the routes: `/build`, `/explore`,
 * `/create`, `/holdings`, `/me/*`, `/watchlist`, `/discover/*`, `/instruments/*` and `/screens/*`
 * all rendered to anybody who typed the URL. A default-open gate cannot be reviewed, because the
 * diff that adds a page never mentions this file.
 *
 * The `next` parameter is what sends the user back where they were going, and it is validated as
 * a *relative path* before use: an open redirect is a phishing primitive, and `?next=https://evil`
 * is how one gets built.
 *
 * **No stored copy of a gated page.** Every gated response carries `Cache-Control: no-store`,
 * which is what makes the browser's Back button honest after a sign-out: with no stored copy and
 * no back/forward-cache entry, going back to `/build` is a fresh request, and a fresh request
 * with no cookie lands on `/login`. Without it the browser would happily re-paint the signed-in
 * page from memory, and the person looking at the screen would have no way to tell.
 *
 * **The CSP.** docs/11 names "Strict CSP (`default-src 'self'`)". A strict policy and a framework
 * that injects inline scripts only coexist through a nonce, so one is generated per request and
 * put in the header; Next reads it back out of the header and stamps it on its own script tags.
 * `strict-dynamic` then lets those scripts load the chunks they need without the policy having to
 * enumerate them.
 *
 * **Two policies, not one.** A nonce is per request; a *statically generated* page is one cached
 * HTML file served to everybody, so its inline script tags cannot carry this request's nonce and
 * a nonce policy would block the page's own bootstrap. docs/08 §Routes asks for `/`, `/faq`,
 * `/about`, `/blog/*` and the legal pages to be SSG, and Prompt 18's first acceptance criterion
 * asks for it again — so those routes get :func:`staticContentSecurityPolicy` instead, which keeps
 * every other directive and relaxes `script-src` to `'self' 'unsafe-inline'`. The trade, and why
 * it is acceptable on exactly those routes and nowhere else, is argued in `docs/DECISIONS.md`
 * §18.2. Everything that renders a session keeps the nonce policy unchanged.
 *
 * The gate is a *convenience*, not the enforcement. Every gated read is authorised again by the
 * API against the bearer token — middleware runs on a cookie the browser sent, and a cookie is
 * not a claim the server should trust on its own. `docs/12a` §11.
 */

/** Auth.js v5 sets one of these depending on whether the deployment is on HTTPS. */
const SESSION_COOKIES = ["authjs.session-token", "__Secure-authjs.session-token"];

/**
 * Staff-only routes are gated here exactly as far as *any* session — Prompt 17 §4. Whether it is a
 * **staff** session is decided by `require_staff` on the API, which answers 404. So a signed-in
 * non-staff user reaches `/admin` and the page 404s, which is the intended behaviour: the
 * middleware is a convenience, not the enforcement (docs/12a §11).
 */
function hasSession(request: NextRequest): boolean {
  return SESSION_COOKIES.some((name) => Boolean(request.cookies.get(name)?.value));
}

/** The two headers Next puts on a `<Link>` prefetch; either one means "not a real navigation". */
function isPrefetch(request: NextRequest): boolean {
  return (
    request.headers.get("next-router-prefetch") === "1" ||
    request.headers.get("purpose") === "prefetch"
  );
}

/**
 * Next appends `?_rsc=<hash>` to the RSC request it makes for a client-side navigation. Echoing it
 * back into `?next=` would send the user, after signing in, to a URL carrying a stale RSC cache
 * key — so it is stripped, and only it: every other query parameter is part of where they were
 * going (`/listings?search=CUPID` is a different destination from `/listings`).
 */
function destination(pathname: string, searchParams: URLSearchParams): string {
  return `${pathname}${search(searchParams)}`;
}

/** The `?…` suffix of that destination, or the empty string. */
function search(searchParams: URLSearchParams): string {
  const carried = new URLSearchParams(searchParams);
  carried.delete("_rsc");
  const query = carried.toString();
  return query ? `?${query}` : "";
}

/**
 * The headers that stop a gated page from being re-painted from a store the server does not
 * control — the browser disk cache, and (because `no-store` disqualifies a page from it) the
 * back/forward cache. `Pragma` and `Expires` are for HTTP/1.0 intermediaries; they cost 30 bytes
 * and they are what a proxy older than the framework understands.
 */
function denyStorage(headers: Headers): void {
  headers.set("cache-control", "no-store, no-cache, must-revalidate, max-age=0");
  headers.set("pragma", "no-cache");
  headers.set("expires", "0");
}

/** A destination we are willing to send someone back to: same-origin, and not another redirect. */
export function safeNext(candidate: string | null): string | null {
  if (!candidate) return null;
  if (!candidate.startsWith("/") || candidate.startsWith("//")) return null;
  return candidate;
}

/** The API's scheme and authority, with any path dropped. Empty when it is not configured. */
function apiOrigin(): string {
  const configured = process.env.NEXT_PUBLIC_API_URL ?? process.env.NEXT_PUBLIC_API_ORIGIN ?? "";
  if (!configured) return "";
  try {
    return new URL(configured).origin;
  } catch {
    return "";
  }
}

/** The directives both policies share. Only `script-src` differs between them. */
function commonDirectives(): string[] {
  return [
    "default-src 'self'",
    // Next injects a `<style>` element for every CSS module it loads, and there is no nonce hook
    // for them. `unsafe-inline` for styles cannot execute code; it is the standard exception.
    "style-src 'self' 'unsafe-inline'",
    // Razorpay checkout loads brand marks from its CDN (AUDIT 2.7).
    "img-src 'self' data: blob: https://cdn.razorpay.com https://*.razorpay.com",
    "font-src 'self' data:",
    // The browser calls `services/api` directly (docs/03 §"Request path for a screen run"), so
    // its origin has to be connectable. `*` would defeat the point of having a policy.
    //
    // The **origin**, not the configured URL. A `connect-src` source that carries a path matches
    // that path only — `http://host/api/v1` permits exactly `/api/v1` and blocks `/api/v1/screens`,
    // which is every call the app actually makes.
    // Razorpay checkout XHR + the API origin the browser talks to (AUDIT 2.7).
    `connect-src 'self' ${apiOrigin()} https://api.razorpay.com https://checkout.razorpay.com https://lumberjack.razorpay.com`.trim(),
    // Checkout renders inside an iframe hosted by Razorpay (AUDIT 2.7).
    "frame-src https://api.razorpay.com https://checkout.razorpay.com",
    "object-src 'none'",
    "base-uri 'self'",
    // The Invest panel POSTs the Kite Publisher basket to kite.zerodha.com/connect/basket.
    // `'self'` alone makes the browser refuse that submit silently (AUDIT 0.2 / 2.3).
    "form-action 'self' https://kite.zerodha.com",
    "frame-ancestors 'none'",
    "upgrade-insecure-requests",
  ];
}

/**
 * The policy for the statically generated public pages.
 *
 * `script-src 'self' 'unsafe-inline'` — the one relaxation. Next inlines its RSC flight payload as
 * `<script>self.__next_f.push(...)</script>` and `next-themes` inlines the no-flash theme script;
 * neither can carry a per-request nonce on a page that is one cached file, and neither can be
 * hashed because the flight payload is content-dependent.
 *
 * What makes it acceptable here and nowhere else: these routes render no session, accept no
 * user-generated content into the DOM, and read nothing from the request. There is no injection
 * source for `'unsafe-inline'` to amplify. Every directive that matters for clickjacking, data
 * exfiltration and base-tag hijacking is unchanged, and the *authenticated* surface — where an
 * injection would actually be worth something — keeps the nonce policy.
 */
export function staticContentSecurityPolicy(isDev: boolean): string {
  return [
    `script-src 'self' 'unsafe-inline' https://checkout.razorpay.com${isDev ? " 'unsafe-eval'" : ""}`,
    ...commonDirectives(),
  ].join("; ");
}

export function contentSecurityPolicy(nonce: string, isDev: boolean): string {
  const directives = [
    // `strict-dynamic` means "trust what the nonced scripts load"; the hashes and host sources
    // after it are ignored by browsers that understand it and are the fallback for those that do
    // not. `unsafe-eval` only in development, where React's refresh runtime needs it.
    //
    // `default-src` is NOT repeated here: `commonDirectives()` already carries it. A policy that
    // names the same directive twice makes the browser honour the first and log
    // "Ignoring duplicate Content-Security-Policy directive 'default-src'" on every page load —
    // console noise that trains a developer to ignore CSP warnings, which is the one class of
    // warning that must stay legible.
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic' https://checkout.razorpay.com${isDev ? " 'unsafe-eval'" : ""}`,
    ...commonDirectives(),
  ];
  return directives.join("; ");
}

export function middleware(request: NextRequest): NextResponse {
  const { pathname, searchParams } = request.nextUrl;
  const gated = !isPublicPath(pathname);

  if (gated && !hasSession(request)) {
    const login = new URL("/login", request.url);
    login.searchParams.set("next", destination(pathname, searchParams));
    const redirect = NextResponse.redirect(login);
    // A cached 307 would keep redirecting after the person signs in.
    denyStorage(redirect.headers);
    return redirect;
  }

  /*
   * A prefetch is where the old matcher stopped short, and it mattered. Next's `<Link>` fetches
   * the RSC payload of a route *before* it is navigated to; the matcher excluded those requests,
   * so a prefetch of a gated route was rendered ungated and parked in the client router cache,
   * and the click that followed was served from that cache without a request ever reaching this
   * file. The gate above therefore runs on prefetches too — and the CSP work below still does
   * not, which is the reason the exclusion existed: a nonce is per request, and stamping this
   * request's nonce into a payload that a *later* document will execute blocks the very scripts
   * it names.
   */
  if (isPrefetch(request)) {
    /*
     * The path headers still go down. They are what tells `(app)/layout.tsx` which page it is
     * rendering, and without them it sees no path at all — which it must read as "gated", or a
     * missing header would be a way past the gate. That would turn an anonymous prefetch of the
     * *public* `/pricing` into a redirect to `/login`, parked in the router cache, and the click
     * that followed would land on the login page for no reason.
     */
    const forwarded = new Headers(request.headers);
    forwarded.set("x-pathname", pathname);
    forwarded.set("x-search", search(searchParams));
    const response = NextResponse.next({ request: { headers: forwarded } });
    if (gated) denyStorage(response.headers);
    return response;
  }

  const isDev = process.env.NODE_ENV !== "production";

  if (isStaticPublicPath(pathname)) {
    // No `x-nonce` on these routes, deliberately: `(marketing)/layout.tsx` must not read the
    // request, or Next opts the whole subtree into dynamic rendering and the pages stop being
    // static. Nothing downstream looks for one.
    const response = NextResponse.next();
    response.headers.set("content-security-policy", staticContentSecurityPolicy(isDev));
    return response;
  }

  const nonce = crypto.randomUUID().replaceAll("-", "");
  const csp = contentSecurityPolicy(nonce, isDev);

  const headers = new Headers(request.headers);
  headers.set("x-nonce", nonce);
  headers.set("content-security-policy", csp);
  /*
   * What the `(app)` layout re-checks the session against. A layout cannot read the URL it is
   * rendering — `headers()` is the documented way through — and it needs the path to know whether
   * this render is the public `/pricing` or a gated page. See `src/app/(app)/layout.tsx`.
   */
  headers.set("x-pathname", pathname);
  headers.set("x-search", search(searchParams));

  const response = NextResponse.next({ request: { headers } });
  response.headers.set("content-security-policy", csp);
  if (gated) denyStorage(response.headers);
  return response;
}

export const config = {
  /*
   * Everything except the framework's own static output and the files it serves verbatim. A CSP
   * on a chunk of JavaScript is bytes on the wire and no protection; the policy that matters is
   * the one on the document that loads it.
   */
  matcher: [
    /*
     * Prefetches are **not** excluded here any more. They used to be, and that hole is what
     * `isPrefetch` above now closes inside the function instead: excluded at the matcher, a
     * prefetch of a gated route never reached the gate at all.
     */
    "/((?!_next/static|_next/image|favicon.ico|robots.txt|sitemap.xml).*)",
  ],
};
