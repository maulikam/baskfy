/**
 * A degraded pipeline degrades the page. It does not kill it.
 *
 * `/market/today` answered a full-page "Application error: a server-side exception has occurred
 * ... Digest: 3649330443" on the live staging host. The backend was not at fault and had said so
 * precisely:
 *
 *     {"type":"pipeline-degraded","status":503,
 *      "detail":"no pipeline_run has been published, so factor_daily has no trustworthy as-of date"}
 *
 * `readJson` threw `MarketDataUnavailable` on any non-2xx, nothing anywhere caught it — the class
 * existed only to be thrown — and Next turned it into a 500. docs/11 §Reliability asks for
 * graceful degradation, so a *state* the API reports politely must not become a crash.
 *
 * The line these draw is where the value is: **503 degrades, everything else still throws.** A
 * catch-all would turn every backend bug into a quiet empty state, which is worse than the crash
 * it replaced because nobody would ever see it.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/api/config", () => ({
  apiOrigin: () => "http://api.test",
  serverApiOrigin: () => "http://api.test",
}));

const { fetchIndexDashboard, fetchIndexDashboardOrDegraded, MarketDataUnavailable } = await import(
  "@/lib/market/fetch"
);

function answer(status: number, body: unknown = {}): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(() =>
      Promise.resolve({
        ok: status >= 200 && status < 300,
        status,
        json: () => Promise.resolve(body),
      } as Response),
    ),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the degrading read", () => {
  it("answers null when the pipeline has published nothing", () => {
    answer(503);
    return expect(fetchIndexDashboardOrDegraded()).resolves.toBeNull();
  });

  it("returns the board when there is one", async () => {
    const board = { as_of: "2026-08-25", data: [{ name: "NIFTY 50" }] };
    answer(200, board);
    await expect(fetchIndexDashboardOrDegraded()).resolves.toEqual(board);
  });

  it("still throws on a 500, because that is a bug and must be loud", () => {
    // The whole reason the catch is narrowed to one status rather than swallowing everything.
    // A server error turned into a quiet empty state is a defect nobody is ever told about, and
    // `no-any.test.ts` scans for exactly that shape — it caught this comment when the example was
    // written literally.
    answer(500);
    return expect(fetchIndexDashboardOrDegraded()).rejects.toBeInstanceOf(MarketDataUnavailable);
  });

  it("still throws on a 404, because a moved route must be loud too", () => {
    answer(404);
    return expect(fetchIndexDashboardOrDegraded()).rejects.toBeInstanceOf(MarketDataUnavailable);
  });
});

describe("the throwing read is unchanged", () => {
  it("still throws on 503 for callers that need the data to exist", async () => {
    // Kept deliberately: a page whose entire subject is the data may reasonably fail without it.
    // Only the callers that can say something useful were moved.
    answer(503);
    await expect(fetchIndexDashboard()).rejects.toBeInstanceOf(MarketDataUnavailable);
  });

  it("names the status in the message, which is how the digest was traced", async () => {
    answer(503);
    await expect(fetchIndexDashboard()).rejects.toThrow(/503/);
  });
});
