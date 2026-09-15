import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));
vi.mock("@/lib/auth", () => ({
  auth: vi.fn(() => Promise.resolve({ accessToken: "tok" })),
}));
vi.mock("@/lib/api/config", () => ({
  serverApiOrigin: () => "http://api.test",
}));

const serverFetchJson = vi.fn();
vi.mock("@/lib/api/server-fetch", () => {
  class ServerFetchStatusError extends Error {
    readonly status: number;
    constructor(url: string, status: number, _problem: Record<string, unknown> | null) {
      super(`${url} responded ${status}`);
      this.status = status;
    }
  }
  class ServerFetchTimeoutError extends Error {
    constructor(path: string, timeoutMs: number) {
      super(`Timed out after ${timeoutMs}ms fetching ${path}`);
    }
  }
  return {
    ServerFetchStatusError,
    ServerFetchTimeoutError,
    serverFetchJson: (...args: unknown[]) => serverFetchJson(...args) as Promise<unknown>,
  };
});

import { ServerFetchStatusError, ServerFetchTimeoutError } from "@/lib/api/server-fetch";
import { DETAIL_FETCH_TIMEOUT_MS, loadPortfolioDetail } from "@/lib/portfolio/detail-fetch";

/**
 * 15 Sep 2026: `/portfolio/6` read "Page not found" on most live refreshes. The summary read hit
 * the 2.5 s shell budget on a busy single-worker API, and a timeout was indistinguishable from a
 * portfolio that does not exist.
 */
describe("loadPortfolioDetail", () => {
  beforeEach(() => {
    serverFetchJson.mockReset();
  });

  it("marks the portfolio missing only when the API answers 404", async () => {
    serverFetchJson.mockRejectedValue(new ServerFetchStatusError("http://api.test/x", 404, null));
    const bundle = await loadPortfolioDetail(9001);
    expect(bundle.detail).toBeNull();
    expect(bundle.missing).toBe(true);
  });

  it("a timeout is a failure to load, not a missing portfolio", async () => {
    serverFetchJson.mockRejectedValue(new ServerFetchTimeoutError("/portfolio/9002", 2500));
    const bundle = await loadPortfolioDetail(9002);
    expect(bundle.detail).toBeNull();
    expect(bundle.missing).toBe(false);
    expect(bundle.failures.detail).toMatch(/did not load/);
  });

  it("a 5xx is a failure to load, not a missing portfolio", async () => {
    serverFetchJson.mockRejectedValue(new ServerFetchStatusError("http://api.test/x", 503, null));
    const bundle = await loadPortfolioDetail(9003);
    expect(bundle.missing).toBe(false);
  });

  it("gives the summary read more than the shell's 2.5 s budget", async () => {
    serverFetchJson.mockResolvedValue({ summary: { name: "Swing Manual" } });
    await loadPortfolioDetail(9004);
    expect(DETAIL_FETCH_TIMEOUT_MS).toBeGreaterThan(2500);
    for (const [options] of serverFetchJson.mock.calls as [{ timeoutMs?: number }][]) {
      expect(options.timeoutMs).toBe(DETAIL_FETCH_TIMEOUT_MS);
    }
  });
});
