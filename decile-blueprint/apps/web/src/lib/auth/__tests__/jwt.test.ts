/**
 * @vitest-environment node
 *
 * Not jsdom: jsdom's `TextEncoder` returns a `Uint8Array` from a different realm, and `jose`'s
 * `instanceof` check rejects it. This code only ever runs on the server or the edge runtime, both
 * of which have the real one, so the node environment is also the honest one to test it in.
 */
import { jwtVerify } from "jose";
import { describe, expect, it } from "vitest";

import {
  ACCESS_TOKEN_TTL_SECONDS,
  assertSecret,
  JWT_ALGORITHM,
  MIN_SECRET_BYTES,
  mintAccessToken,
} from "@/lib/auth/jwt";

/**
 * The token this app mints has to satisfy `decile_api.auth.decode_token`, which is deliberately
 * strict (docs/07a §11):
 *
 *   * one algorithm, HS256, with no negotiation;
 *   * `sub`, `iat` and `exp` all required;
 *   * `exp - iat` no greater than the API's ceiling — 15 minutes (docs/11 §Security), enforced
 *     *even for a correctly signed token*.
 *
 * So these are not tests of `jose`. They are tests that the two halves of the contract agree, and
 * they are the cheapest place to catch a drift that would otherwise surface as a 401 on every
 * authenticated request.
 */
const SECRET = "decile-test-secret-0123456789abc";

describe("the access token", () => {
  it("is HS256", async () => {
    const { token } = await mintAccessToken({ subject: "abc123def456" }, SECRET);
    const header = JSON.parse(
      Buffer.from(token.split(".")[0] as string, "base64url").toString("utf-8"),
    ) as { alg: string };
    expect(header.alg).toBe(JWT_ALGORITHM);
  });

  it("carries sub, iat and exp, because the API requires all three", async () => {
    const issuedAt = new Date("2026-08-18T09:00:00Z");
    const { token } = await mintAccessToken({ subject: "abc123def456", issuedAt }, SECRET);
    // Verified *as at* the issue time. A fixed timestamp keeps the assertion deterministic; the
    // alternative — minting relative to `now` — would make the test pass for the wrong reason on
    // any day, including one where the TTL had been changed to a week.
    const { payload } = await jwtVerify(token, new TextEncoder().encode(SECRET), {
      currentDate: issuedAt,
    });

    expect(payload.sub).toBe("abc123def456");
    expect(payload.iat).toBe(Math.floor(issuedAt.getTime() / 1000));
    expect(payload.exp).toBe(Math.floor(issuedAt.getTime() / 1000) + ACCESS_TOKEN_TTL_SECONDS);
  });

  it("lives exactly fifteen minutes", async () => {
    // docs/11 §Security: "15-min access". Longer is refused by the API; shorter would churn.
    expect(ACCESS_TOKEN_TTL_SECONDS).toBe(15 * 60);
    const { token, expiresAt } = await mintAccessToken({ subject: "abc123def456" }, SECRET);
    const { payload } = await jwtVerify(token, new TextEncoder().encode(SECRET));
    expect(expiresAt).toBe((payload.exp as number) * 1000);
    expect((payload.exp as number) - (payload.iat as number)).toBe(ACCESS_TOKEN_TTL_SECONDS);
  });

  it("sets iss and aud only when they are configured", async () => {
    const bare = await mintAccessToken({ subject: "abc" }, SECRET);
    const { payload: barePayload } = await jwtVerify(
      bare.token,
      new TextEncoder().encode(SECRET),
    );
    expect(barePayload.iss).toBeUndefined();
    expect(barePayload.aud).toBeUndefined();

    const scoped = await mintAccessToken(
      { subject: "abc", issuer: "decile-web", audience: "decile-api" },
      SECRET,
    );
    const { payload } = await jwtVerify(scoped.token, new TextEncoder().encode(SECRET), {
      issuer: "decile-web",
      audience: "decile-api",
    });
    expect(payload.iss).toBe("decile-web");
  });

  it("refuses to sign with no secret at all", async () => {
    await expect(mintAccessToken({ subject: "abc" }, undefined)).rejects.toThrow(
      /DECILE_JWT_SECRET is not set/,
    );
  });

  it("refuses a secret shorter than RFC 7518 §3.2 allows for HS256", () => {
    // The API's `Settings.require_configured()` enforces the same floor. Both sides, or neither.
    expect(MIN_SECRET_BYTES).toBe(32);
    expect(() => assertSecret("too-short")).toThrow(/shorter than the 32 bytes/);
    expect(assertSecret(SECRET)).toBe(SECRET);
  });
});
