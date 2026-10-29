/**
 * @vitest-environment node
 *
 * Node, not jsdom, for the reason `jwt.test.ts` gives: `jose` rejects jsdom's TextEncoder.
 */
import { decodeJwt } from "jose";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { localSignInPublicId, signInLocally } from "@/lib/auth/api-auth";

/**
 * Local sign-in (Maulik, 29 Oct 2026: "since it is on localhost, no google login required").
 *
 * What it must guarantee, whatever the implementation:
 *   * it exists only where the deployment names the account — unset means no local sign-in;
 *   * the session it issues carries the account's REAL `session_epoch`, read from the API, so a
 *     revocation still ends it exactly as it ends a Google session;
 *   * it is no sign-in at all when the API refuses the account or cannot be reached.
 */
const SECRET = "baskfy-test-secret-0123456789abc";
const PUBLIC_ID = "2fe482ed2015";

function meResponse(body: Record<string, unknown>, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

beforeEach(() => {
  vi.stubEnv("BASKFY_INTERNAL_API_ORIGIN", "http://api:8000");
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe("localSignInPublicId", () => {
  it("is undefined unless the deployment names the account", () => {
    vi.stubEnv("BASKFY_LOCAL_SIGNIN_PUBLIC_ID", "");
    expect(localSignInPublicId()).toBeUndefined();
    vi.stubEnv("BASKFY_LOCAL_SIGNIN_PUBLIC_ID", "   ");
    expect(localSignInPublicId()).toBeUndefined();
  });

  it("is the configured public id when set", () => {
    vi.stubEnv("BASKFY_LOCAL_SIGNIN_PUBLIC_ID", ` ${PUBLIC_ID} `);
    expect(localSignInPublicId()).toBe(PUBLIC_ID);
  });
});

describe("signInLocally", () => {
  it("stamps the session with the epoch the API reports, not the probe's", async () => {
    const fetchMock = vi.fn(async () =>
      meResponse({ public_id: PUBLIC_ID, email: "op@example.com", name: "Op", session_epoch: 3 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const account = await signInLocally(PUBLIC_ID, SECRET, undefined, undefined);

    expect(account).not.toBeNull();
    expect(account?.sessionEpoch).toBe(3);
    expect(account?.email).toBe("op@example.com");
    const claims = decodeJwt(account?.accessToken ?? "");
    expect(claims.sub).toBe(PUBLIC_ID);
    expect(claims.epoch).toBe(3);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://api:8000/api/v1/me",
      expect.objectContaining({ cache: "no-store" }),
    );
  });

  it("is no sign-in when the API refuses the account", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => meResponse({ detail: "unauthorized" }, 401)));
    expect(await signInLocally(PUBLIC_ID, SECRET, undefined, undefined)).toBeNull();
  });

  it("is no sign-in when the API answers for a different account", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => meResponse({ public_id: "someone-else", email: "x@example.com" })),
    );
    expect(await signInLocally(PUBLIC_ID, SECRET, undefined, undefined)).toBeNull();
  });

  it("is no sign-in when the API is unreachable", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("fetch failed");
      }),
    );
    expect(await signInLocally(PUBLIC_ID, SECRET, undefined, undefined)).toBeNull();
  });
});
