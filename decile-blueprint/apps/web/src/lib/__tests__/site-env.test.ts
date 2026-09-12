/**
 * Production refuses silent fallbacks for public URLs and the revalidate secret (AUDIT 2.9).
 */
import { afterEach, describe, expect, it, vi } from "vitest";

async function loadSite() {
  // Module-level SITE_URL / DESK_CONSOLE_URL evaluate on import — set them first.
  vi.stubEnv("NEXT_PUBLIC_SITE_URL", "https://baskfy.com");
  vi.stubEnv("NEXT_PUBLIC_DESK_URL", "https://desk.example");
  vi.stubEnv("REVALIDATE_SECRET", "test-secret");
  return import("@/lib/site");
}

describe("requiredPublicUrl / assertRevalidateSecretConfigured", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  it("throws in production when the public URL argument is unset", async () => {
    vi.stubEnv("NODE_ENV", "production");
    const { requiredPublicUrl } = await loadSite();
    expect(() => requiredPublicUrl("NEXT_PUBLIC_SITE_URL", undefined, "http://localhost:3000")).toThrow(
      /NEXT_PUBLIC_SITE_URL must be set in production/,
    );
  });

  it("throws in production when NEXT_PUBLIC_DESK_URL argument is unset", async () => {
    vi.stubEnv("NODE_ENV", "production");
    const { requiredPublicUrl } = await loadSite();
    expect(() =>
      requiredPublicUrl("NEXT_PUBLIC_DESK_URL", undefined, "https://desk.modelbasket.in"),
    ).toThrow(/NEXT_PUBLIC_DESK_URL must be set in production/);
  });

  it("throws in production when REVALIDATE_SECRET is unset", async () => {
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("NEXT_PUBLIC_SITE_URL", "https://baskfy.com");
    vi.stubEnv("NEXT_PUBLIC_DESK_URL", "https://desk.example");
    vi.stubEnv("REVALIDATE_SECRET", "");
    vi.resetModules();
    const { assertRevalidateSecretConfigured } = await import("@/lib/site");
    expect(() => assertRevalidateSecretConfigured()).toThrow(
      /REVALIDATE_SECRET must be set in production/,
    );
  });

  it("allows the dev fallback when NODE_ENV is not production", async () => {
    vi.stubEnv("NODE_ENV", "development");
    const { requiredPublicUrl } = await loadSite();
    expect(requiredPublicUrl("NEXT_PUBLIC_DESK_URL", undefined, "https://desk.modelbasket.in")).toBe(
      "https://desk.modelbasket.in",
    );
  });
});
