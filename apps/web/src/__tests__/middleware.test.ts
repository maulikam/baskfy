// @vitest-environment node
import { NextRequest } from "next/server";
import { describe, expect, it } from "vitest";

import { middleware, safeNext } from "@/middleware";

/**
 * The gate, exercised as a request/response pair rather than as a predicate — because the two
 * things that went wrong historically were both about *requests*, not about the route list.
 *
 * The first was the default: an enumerated list of gated prefixes meant a new page was public
 * until somebody edited the middleware. The second was subtler and worse. The matcher excluded
 * `<Link>` prefetches, so a prefetch of a gated route never reached the gate at all: Next rendered
 * it, parked the payload in the client router cache, and the click that followed was served from
 * that cache. Both have a test here, and the prefetch one is the reason this file exists.
 */
const SESSION = "authjs.session-token=a-session-token";

function request(path: string, headers: Record<string, string> = {}): NextRequest {
  return new NextRequest(new URL(path, "https://baskfy.com"), { headers: new Headers(headers) });
}

function signedIn(path: string, headers: Record<string, string> = {}): NextRequest {
  return request(path, { ...headers, cookie: SESSION });
}

/** Where a redirect response points, as a path + query. */
function target(location: string | null): string {
  if (!location) throw new Error("expected a Location header");
  const url = new URL(location, "https://baskfy.com");
  return `${url.pathname}${url.search}`;
}

describe("an anonymous request for a gated page", () => {
  it.each([
    "/build",
    "/explore",
    "/holdings",
    "/portfolio/portfolios",
    "/instruments/CUPID",
    "/market-health",
    "/dashboard",
    "/listings",
    "/profile",
    "/admin/users",
  ])("%s is redirected to the login page", (path) => {
    const response = middleware(request(path));
    expect(response.status).toBe(307);
    expect(target(response.headers.get("location"))).toBe(`/login?next=${encodeURIComponent(path)}`);
  });

  it("carries the query string, because it is part of where they were going", () => {
    const response = middleware(request("/listings?search=CUPID"));
    expect(target(response.headers.get("location"))).toBe(
      `/login?next=${encodeURIComponent("/listings?search=CUPID")}`,
    );
  });

  it("drops Next's `_rsc` cache key, which is not part of the destination", () => {
    const response = middleware(request("/build?tab=filters&_rsc=1f2a3"));
    expect(target(response.headers.get("location"))).toBe(
      `/login?next=${encodeURIComponent("/build?tab=filters")}`,
    );
  });

  it("is not itself cacheable — a stored 307 would keep redirecting after signing in", () => {
    const response = middleware(request("/build"));
    expect(response.headers.get("cache-control")).toContain("no-store");
  });
});

describe("a prefetch is gated too", () => {
  /**
   * The hole this closes: with the prefetch excluded, `<Link href="/build">` on a public page
   * fetched and cached the signed-in payload of `/build` for a visitor who had never signed in,
   * and clicking it navigated from that cache without a request.
   */
  it.each([{ "next-router-prefetch": "1" }, { purpose: "prefetch" }])(
    "%o still meets the gate",
    (headers) => {
      const response = middleware(request("/build", headers));
      expect(response.status).toBe(307);
      expect(target(response.headers.get("location"))).toContain("/login");
    },
  );

  it("does not stamp a nonce on a prefetched payload a later document would execute", () => {
    const response = middleware(signedIn("/build", { "next-router-prefetch": "1" }));
    expect(response.headers.get("content-security-policy")).toBeNull();
    expect(response.headers.get("cache-control")).toContain("no-store");
  });

  it("still passes the path down, or the layout would read a public page as gated", () => {
    /* Without this the anonymous prefetch of `/pricing` below reaches `(app)/layout.tsx` with no
       path, the layout reads "I do not know which page this is" as gated — which it must — and
       parks a redirect to `/login` in the router cache for a page that is public. */
    const response = middleware(request("/pricing", { purpose: "prefetch" }));
    expect(response.status).toBe(200);
    expect(response.headers.get("x-middleware-request-x-pathname")).toBe("/pricing");
    expect(response.headers.get("cache-control")).toBeNull();
  });
});

describe("a public page needs no session", () => {
  it.each([
    "/",
    "/pricing",
    "/faq",
    "/blog/what-a-decile-actually-measures",
    "/privacy-policy",
    "/login",
    "/alerts/unsubscribe?token=abc",
    "/api/auth/session",
  ])("%s is served, not redirected", (path) => {
    const response = middleware(request(path));
    expect(response.status).toBe(200);
    expect(response.headers.get("location")).toBeNull();
  });

  it("is allowed to be stored — these pages carry nothing private", () => {
    expect(middleware(request("/faq")).headers.get("cache-control")).toBeNull();
  });
});

describe("a signed-in request for a gated page", () => {
  it("is served, and told not to store the result anywhere", () => {
    const response = middleware(signedIn("/build"));
    expect(response.status).toBe(200);
    expect(response.headers.get("location")).toBeNull();
    /* This is what makes the Back button honest after a sign-out: no stored copy, no
       back/forward-cache entry, so going back is a real request and a real request has no cookie. */
    expect(response.headers.get("cache-control")).toContain("no-store");
    expect(response.headers.get("pragma")).toBe("no-cache");
  });

  it("still gets the nonce policy", () => {
    const csp = middleware(signedIn("/build")).headers.get("content-security-policy");
    expect(csp).toMatch(/script-src 'self' 'nonce-[a-f0-9]+' 'strict-dynamic'/);
  });

  it("passes the path down, which is how the layout re-checks the session server-side", () => {
    const response = middleware(signedIn("/listings?search=CUPID"));
    /* Next carries a middleware's request-header rewrites on the response under this prefix, and
       replays them into the render. `(app)/layout.tsx` reads them back out of `headers()`; a
       layout has no other way to learn which URL it is rendering. */
    expect(response.headers.get("x-middleware-request-x-pathname")).toBe("/listings");
    expect(response.headers.get("x-middleware-request-x-search")).toBe("?search=CUPID");
  });
});

describe("safeNext refuses an open redirect", () => {
  it.each(["https://evil.example/phishing", "//evil.example", "javascript:alert(1)", ""])(
    "%s is refused",
    (candidate) => {
      expect(safeNext(candidate)).toBeNull();
    },
  );

  it("accepts a same-origin path", () => {
    expect(safeNext("/build?tab=filters")).toBe("/build?tab=filters");
  });
});
