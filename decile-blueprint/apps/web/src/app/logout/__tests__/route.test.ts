import { NextRequest } from "next/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

/**
 * What a sign-out has to leave behind, asserted as the *spec* rather than as whatever the route
 * happens to emit (house rule 2).
 *
 * The behaviour under test is not "the cookie is gone" — Auth.js owns that and testing it here
 * would be testing a dependency. It is the part this route added because Auth.js does not do it:
 * a response that tells the browser to drop the origin's stores, the back/forward cache among
 * them. That is the control standing between a signed-out person and the Back button re-painting
 * their holdings from memory, and it lives in exactly one header.
 */

const signOut = vi.fn<(options: { redirect: boolean }) => Promise<undefined>>(() =>
  Promise.resolve(undefined),
);
vi.mock("@/lib/auth", () => ({
  signOut: (options: { redirect: boolean }) => signOut(options),
}));

async function signOutResponse(): Promise<Response> {
  const { GET } = await import("../route");
  return GET(new NextRequest("https://staging.baskfy.com/logout"));
}

describe("GET /logout", () => {
  beforeEach(() => {
    signOut.mockClear();
  });

  it("tells the browser to drop the origin's stores, including the back/forward cache", async () => {
    const response = await signOutResponse();
    const directive = response.headers.get("clear-site-data") ?? "";

    /* `cache` is the one that reaches bfcache; without it Chrome may restore a `no-store` gated
       page from memory after the cookie is already gone. */
    expect(directive).toContain('"cache"');
    expect(directive).toContain('"cookies"');
    expect(directive).toContain('"storage"');
  });

  it("is never stored itself, because a cached sign-out signs out once", async () => {
    const response = await signOutResponse();
    expect(response.headers.get("cache-control")).toContain("no-store");
  });

  it("sends the browser home with a method-resetting redirect", async () => {
    const response = await signOutResponse();
    /* 303, so a sign-out that becomes a form post one day does not replay POST at `/`. */
    expect(response.status).toBe(303);
    /*
     * Relative, not absolute — and this assertion is the one that would have caught the bug.
     *
     * It used to expect `https://staging.baskfy.com/`, which passed because the request above is
     * constructed with that host. On the deployed box the Next server binds to `0.0.0.0:3000` and
     * the handler resolved `nextUrl.origin` from the bind address, so a real sign-out sent the
     * browser to `https://0.0.0.0:3000/`. The old test asserted what a well-formed request does,
     * never what the container does; a relative `Location` cannot express the difference, which is
     * why the fix and the assertion are the same shape (house rule 2 — assert the spec).
     */
    expect(response.headers.get("location")).toBe("/");
  });

  it("clears the session cookie under both of the names Auth.js uses", async () => {
    const response = await signOutResponse();
    const setCookie = response.headers.getSetCookie().join("\n");

    expect(setCookie).toContain("authjs.session-token=");
    expect(setCookie).toContain("__Secure-authjs.session-token=");
  });

  it("still asks Auth.js to end the session, and owns the redirect itself", async () => {
    await signOutResponse();
    /* `redirect: false` is what leaves this route holding a response it can put headers on. A
       `redirectTo` would throw NEXT_REDIRECT and the Clear-Site-Data header would never exist. */
    expect(signOut).toHaveBeenCalledWith({ redirect: false });
  });
});
