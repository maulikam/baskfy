/**
 * AUDIT 2.14 — HSTS preload is production-only (not staging).
 *
 * next.config evaluates `NEXT_PUBLIC_SITE_URL` at load time, so each case reloads the module.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

describe("next.config security headers", () => {
  afterEach(() => {
    vi.resetModules();
    vi.unstubAllEnvs();
  });

  it("omits Strict-Transport-Security when the site URL is staging", async () => {
    vi.stubEnv("NEXT_PUBLIC_SITE_URL", "https://staging.baskfy.com");
    const { default: config } = await import("../../../next.config");
    const headers = await config.headers?.();
    const flat = (headers ?? []).flatMap((entry) => entry.headers);
    expect(flat.some((h) => h.key === "Strict-Transport-Security")).toBe(false);
  });

  it("sends HSTS preload on a non-staging production site URL", async () => {
    vi.stubEnv("NEXT_PUBLIC_SITE_URL", "https://baskfy.com");
    const { default: config } = await import("../../../next.config");
    const headers = await config.headers?.();
    const flat = (headers ?? []).flatMap((entry) => entry.headers);
    const hsts = flat.find((h) => h.key === "Strict-Transport-Security");
    expect(hsts?.value).toContain("preload");
  });
});
