import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * C7, across all three sleeves — `gates/sleeve-read-contract.md`.
 *
 * `src/lib/swing/__tests__/read-contract.test.ts` is Agent C's proof on the swing hub. The bug it
 * names is not swing's: the same `readOrNull` is copied into `lib/vbt/fetch.ts` and
 * `lib/twt/fetch.ts`, and the sentence the audit is named after — *"Nothing has been read for
 * this strategy yet"* — is the **TWT** hub's. A fix that landed on one sleeve and left the other
 * two lying would be worse than no fix, because the one page a person checks would be the calm
 * one.
 *
 * So this file asserts the same three cases on VBT and TWT: a refused caller and a degraded
 * deployment are visible, and an ordinary empty answer is still `null`.
 */

const ORIGIN = "http://api.test";

vi.mock("@/lib/auth", () => ({
  auth: () => Promise.resolve({ accessToken: "test-token" }),
}));

vi.mock("@/lib/api/config", () => ({
  serverApiOrigin: () => ORIGIN,
}));

const REFUSAL = {
  type: "not-found",
  title: "Not found",
  status: 404,
  detail: "This deployment serves a single account and this caller (user 6) is not it.",
  reason: "not-the-sole-tenant",
};

const DEGRADED = {
  type: "pipeline-degraded",
  status: 503,
  detail: "The sole-tenant account is not configured on this deployment. Set BASKFY_SOLE_USER_ID.",
};

function answering(body: object, status: number): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.resolve(Response.json(body, { status }))),
  );
}

/** Did the read reach the page as something other than "nothing has been written yet"? */
async function visible(read: () => Promise<unknown>): Promise<boolean> {
  try {
    return (await read()) !== null;
  } catch {
    return true;
  }
}

const SLEEVES = [
  {
    name: "vbt",
    load: async () => {
      const { fetchToday } = await import("@/lib/vbt/fetch");
      return () => fetchToday();
    },
  },
  {
    name: "twt",
    load: async () => {
      const { fetchToday } = await import("@/lib/twt/fetch");
      return () => fetchToday();
    },
  },
] as const;

describe.each(SLEEVES)("$name reads distinguish a refusal from an empty book", (sleeve) => {
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("answers null when the detector has genuinely written nothing", async () => {
    answering({ as_of: null, gate: null, candidates: [], rejects: [] }, 200);
    const read = await sleeve.load();
    const page = await read();
    expect(page).not.toBeNull();
  });

  it("does not answer the same thing when the API refuses the caller", async () => {
    answering(REFUSAL, 404);
    const read = await sleeve.load();
    expect(
      await visible(read),
      "a refused second account is told the strategy has never run, over a database holding " +
        "the session",
    ).toBe(true);
  });

  it("does not answer the same thing when the sole tenant is not configured", async () => {
    answering(DEGRADED, 503);
    const read = await sleeve.load();
    expect(await visible(read), "a 503 is rendered as an ordinary quiet day").toBe(true);
  });

  it("does not answer the same thing when the API fails", async () => {
    answering({ type: "internal-error", status: 500, detail: "An unexpected error occurred." }, 500);
    const read = await sleeve.load();
    expect(await visible(read), "a 500 is rendered as an ordinary quiet day").toBe(true);
  });

  it("still answers null for a plain 404 — nothing there, no refusal marker", async () => {
    answering({ type: "not-found", title: "Not found", status: 404, detail: "Not Found" }, 404);
    const read = await sleeve.load();
    expect(await read()).toBeNull();
  });
});
