import { NextResponse, type NextRequest } from "next/server";

import { isStaticPublicPath } from "@/lib/marketing/routes";

/**
 * Two jobs, both from docs/11 §Security (Prompt 12 deliverables 2 and 4).
 *
 * **The gate.** `/profile` and `/change-password` are about one account and mean nothing without
 * one. An unauthenticated visit is redirected to `/login?next=…` rather than rendered empty, and
 * the `next` parameter is what sends the user back where they were going. It is validated as a
 * *relative path* before use: an open redirect is a phishing primitive, and `?next=https://evil`
 * is how one gets built.
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

/** Routes that are meaningless without an account. `/screens` is not one: examples read publicly. */
const GATED_PREFIXES = [
  "/profile",
  "/change-password",
  "/portfolios",
  "/me/portfolios",
  "/backtests",
  "/build/backtests",
  "/invoices",
  // Prompt 17 §4. This only checks that *a* session cookie exists; whether it is a **staff**
  // session is decided by `require_staff` on the API, which answers 404. So a signed-in
  // non-staff user reaches the page and the page 404s, which is the intended behaviour — the
  // middleware is a convenience, not the enforcement (docs/12a §11).
  "/admin",
];

/** Auth.js v5 sets one of these depending on whether the deployment is on HTTPS. */
const SESSION_COOKIES = ["authjs.session-token", "__Secure-authjs.session-token"];

function isGated(pathname: string): boolean {
  return GATED_PREFIXES.some(
    (prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`),
  );
}

function hasSession(request: NextRequest): boolean {
  return SESSION_COOKIES.some((name) => Boolean(request.cookies.get(name)?.value));
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
    "img-src 'self' data: blob:",
    "font-src 'self' data:",
    // The browser calls `services/api` directly (docs/03 §"Request path for a screen run"), so
    // its origin has to be connectable. `*` would defeat the point of having a policy.
    //
    // The **origin**, not the configured URL. A `connect-src` source that carries a path matches
    // that path only — `http://host/api/v1` permits exactly `/api/v1` and blocks `/api/v1/screens`,
    // which is every call the app actually makes.
    `connect-src 'self' ${apiOrigin()}`.trim(),
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
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
    `script-src 'self' 'unsafe-inline'${isDev ? " 'unsafe-eval'" : ""}`,
    ...commonDirectives(),
  ].join("; ");
}

function contentSecurityPolicy(nonce: string, isDev: boolean): string {
  const directives = [
    "default-src 'self'",
    // `strict-dynamic` means "trust what the nonced scripts load"; the hashes and host sources
    // after it are ignored by browsers that understand it and are the fallback for those that do
    // not. `unsafe-eval` only in development, where React's refresh runtime needs it.
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${isDev ? " 'unsafe-eval'" : ""}`,
    ...commonDirectives(),
  ];
  return directives.join("; ");
}

export function middleware(request: NextRequest): NextResponse {
  const { pathname, search } = request.nextUrl;

  if (isGated(pathname) && !hasSession(request)) {
    const login = new URL("/login", request.url);
    login.searchParams.set("next", `${pathname}${search}`);
    return NextResponse.redirect(login);
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

  const response = NextResponse.next({ request: { headers } });
  response.headers.set("content-security-policy", csp);
  return response;
}

export const config = {
  /*
   * Everything except the framework's own static output and the files it serves verbatim. A CSP
   * on a chunk of JavaScript is bytes on the wire and no protection; the policy that matters is
   * the one on the document that loads it.
   */
  matcher: [
    {
      source: "/((?!_next/static|_next/image|favicon.ico|robots.txt|sitemap.xml).*)",
      missing: [
        { type: "header", key: "next-router-prefetch" },
        { type: "header", key: "purpose", value: "prefetch" },
      ],
    },
  ],
};
