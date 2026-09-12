/**
 * AUDIT 2.11 — broker OAuth callback redirects prefer NEXT_PUBLIC_SITE_URL.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/auth", () => ({
  auth: () => Promise.resolve(null),
}));

describe("brokers callback redirect origin", () => {
  afterEach(() => {
    vi.resetModules();
    vi.unstubAllEnvs();
  });

  it("uses NEXT_PUBLIC_SITE_URL even when X-Forwarded-Host disagrees", async () => {
    vi.stubEnv("NEXT_PUBLIC_SITE_URL", "https://staging.baskfy.com");
    const { GET } = await import("@/app/api/v1/brokers/callback/route");
    const request = new Request(
      "http://0.0.0.0:3000/api/v1/brokers/callback?request_token=tok&state=st",
      {
        headers: {
          "x-forwarded-host": "evil.example",
          "x-forwarded-proto": "https",
        },
      },
    );
    const response = await GET(request);
    // Unauthenticated → redirect to login on the configured site, not the forwarded host.
    expect(response.status).toBeGreaterThanOrEqual(300);
    expect(response.status).toBeLessThan(400);
    const location = response.headers.get("location") ?? "";
    expect(location.startsWith("https://staging.baskfy.com/")).toBe(true);
    expect(location).not.toContain("evil.example");
  });
});
