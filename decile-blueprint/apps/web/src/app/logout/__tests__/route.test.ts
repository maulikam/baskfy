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
 *
 * AUDIT 2.10: the mutation is POST; GET only serves an auto-submit form.
 */

const signOut = vi.fn<(options: { redirect: boolean }) => Promise<undefined>>(() =>
  Promise.resolve(undefined),
);
vi.mock("@/lib/auth", () => ({
  signOut: (options: { redirect: boolean }) => signOut(options),
}));

async function signOutResponse(): Promise<Response> {
  const { POST } = await import("../route");
  return POST(new NextRequest("https://staging.baskfy.com/logout", { method: "POST" }));
}

describe("POST /logout", () => {
  beforeEach(() => {
    signOut.mockClear();
  });

  it("tells the browser to drop the origin's stores, including the back/forward cache", async () => {
    const response = await signOutResponse();
    const directive = response.headers.get("clear-site-data") ?? "";

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
    expect(response.status).toBe(303);
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
    expect(signOut).toHaveBeenCalledWith({ redirect: false });
  });
});

describe("GET /logout", () => {
  beforeEach(() => {
    signOut.mockClear();
  });

  it("does not clear the session itself — it posts a form so the mutation is POST", async () => {
    const { GET } = await import("../route");
    const response = GET(new NextRequest("https://staging.baskfy.com/logout"));
    expect(response.status).toBe(200);
    expect(response.headers.get("clear-site-data")).toBeNull();
    const body = await response.text();
    expect(body).toContain('method="post"');
    expect(body).toContain('action="/logout"');
    expect(signOut).not.toHaveBeenCalled();
  });
});
