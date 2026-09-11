import { describe, expect, it, vi } from "vitest";

import type { CreateResult } from "@/lib/portfolio/create";
import type { PortfolioDraft } from "@/lib/portfolio/organize";

/**
 * `createPortfolioAction`'s one guard, and the path it must not stand in front of.
 *
 * It refused EVERY add-to-existing for having no name, before the request reached the route, and
 * the refusal was then rendered on a step that path never visits — so the button did nothing
 * visible at all. A portfolio that already exists already has a name; the flow never asks.
 */

const createPortfolio = vi.fn<(draft: PortfolioDraft) => Promise<CreateResult>>();
vi.mock("@/lib/portfolio/create", () => ({
  createPortfolio: (draft: PortfolioDraft) => createPortfolio(draft),
}));
vi.mock("next/cache", () => ({ revalidatePath: vi.fn() }));

/* The module also carries PC6's six management writes, which reach the API through `serverApi()`
   — and that pulls `auth()` and next-auth's server entry into a jsdom run, where it does not
   resolve. Mocked at the seam rather than by splitting the file: these actions belong beside
   `createPortfolioAction` because they are the same kind of thing, and a test that cannot import
   its own module is not evidence about anything. */
const apiCalls: Array<{ method: string; path: string; init: unknown }> = [];
const apiResponse = { ok: true as boolean, data: {} as unknown, error: undefined as unknown };
function record(method: string) {
  return (path: string, init: unknown) => {
    apiCalls.push({ method, path, init });
    return Promise.resolve({
      data: apiResponse.ok ? apiResponse.data : undefined,
      error: apiResponse.error,
      response: { ok: apiResponse.ok },
    });
  };
}
vi.mock("@/lib/api/server", () => ({
  serverApi: () =>
    Promise.resolve({
      GET: record("GET"),
      POST: record("POST"),
      PATCH: record("PATCH"),
      PUT: record("PUT"),
      DELETE: record("DELETE"),
    }),
}));

const {
  createPortfolioAction,
  deletePortfolioAction,
  loadSleevesAction,
  renamePortfolioAction,
  transferHoldingsAction,
} = await import("@/app/actions/portfolio");

function draft(overrides: Partial<PortfolioDraft> = {}): PortfolioDraft {
  return {
    start: "HOLDINGS",
    kind: "CAPITAL",
    name: "",
    benchmark: "",
    keys: [{ instrument_id: 1, broker_account_id: 1 }],
    sourceId: null,
    ...overrides,
  };
}

describe("createPortfolioAction", () => {
  it("still refuses a nameless NEW portfolio", async () => {
    createPortfolio.mockResolvedValue({ ok: true, portfolioId: 1 });

    const result = await createPortfolioAction(draft());

    expect(result).toEqual({ ok: false, reason: "Give the portfolio a name first." });
    expect(createPortfolio).not.toHaveBeenCalled();
  });

  it("does NOT require a name when adding to a portfolio that already has one", async () => {
    createPortfolio.mockResolvedValue({ ok: true, portfolioId: 7 });

    const result = await createPortfolioAction(
      draft({ start: "EXISTING", targetPortfolioId: 7 }),
    );

    expect(createPortfolio).toHaveBeenCalledTimes(1);
    expect(result).toEqual({ ok: true, portfolioId: 7 });
  });
});

/* ------------------------------------------------------------------ *
 * The six writes PC6's drawer calls — `gates/pc-integration.md` I1
 * ------------------------------------------------------------------ *
 *
 * The drawer disables any control whose handler the page did not pass, so an unwired action is
 * visible rather than silent. What is NOT visible is an action wired to the wrong route or one
 * that reports a failure as a success, and that is what these hold.
 */

function resetApi(ok = true, data: unknown = { id: 7 }, error: unknown = undefined) {
  apiCalls.length = 0;
  apiResponse.ok = ok;
  apiResponse.data = data;
  apiResponse.error = error;
}

describe("the management drawer's writes", () => {
  it("actions: renaming sends the trimmed name to the portfolio's own route", async () => {
    resetApi();
    const outcome = await renamePortfolioAction(7, "  Long-Term Wealth  ");

    expect(outcome.ok).toBe(true);
    expect(apiCalls).toHaveLength(1);
    expect(apiCalls[0]!.method).toBe("PATCH");
    expect(apiCalls[0]!.path).toBe("/api/v1/portfolios/{portfolio_id}");
    expect(apiCalls[0]!.init).toMatchObject({ body: { name: "Long-Term Wealth" } });
  });

  it("fail: a refused rename reports the SERVER's sentence, not a generic one", async () => {
    resetApi(false, undefined, { detail: "A portfolio called Long-Term Wealth already exists." });
    const outcome = await renamePortfolioAction(7, "Long-Term Wealth");

    expect(outcome).toEqual({
      ok: false,
      reason: "A portfolio called Long-Term Wealth already exists.",
    });
  });

  it("fail: a refusal with no sentence still gets one, and it says nothing changed", async () => {
    resetApi(false, undefined, {});
    const outcome = await transferHoldingsAction({
      intent: "MOVE",
      destinationPortfolioId: 3,
      sourcePortfolioId: 1,
      holdings: [{ instrument_id: 11, broker_account_id: 1, quantity: "5" }],
    });

    expect(outcome.ok).toBe(false);
    expect(outcome.ok === false && outcome.reason).toMatch(/exactly as they were/);
  });

  it("actions: a transfer never sends a source portfolio, because the route has no such field", () => {
    /* `_apply_allocation` takes the free shares first and then the smallest other slice. Sending
       a "from" would be a value silently ignored, which is worse than not sending it: it would
       read like a constraint the server honours. */
    resetApi();
    return transferHoldingsAction({
      intent: "MOVE",
      destinationPortfolioId: 3,
      sourcePortfolioId: 1,
      holdings: [{ instrument_id: 11, broker_account_id: 1, quantity: "5" }],
    }).then(() => {
      expect(apiCalls[0]!.path).toBe("/api/v1/portfolio/{portfolio_id}/holdings");
      expect(JSON.stringify(apiCalls[0]!.init)).not.toMatch(/source/i);
    });
  });

  it("actions: an empty selection is refused before it reaches the route", async () => {
    resetApi();
    const outcome = await transferHoldingsAction({
      intent: "ASSIGN",
      destinationPortfolioId: 3,
      sourcePortfolioId: null,
      holdings: [],
    });

    expect(outcome.ok).toBe(false);
    expect(apiCalls).toHaveLength(0);
  });

  it("actions: sleeves are read off `sleeves`, not a `data` envelope this route does not send", async () => {
    resetApi(true, { sleeves: [{ id: 1, name: "Core" }], total_capital: "100" });
    const rows = await loadSleevesAction(7);

    expect(rows).toHaveLength(1);
  });

  it("state: an unreadable sleeve list is empty rather than a thrown page", async () => {
    resetApi(false, undefined, { detail: "no" });
    expect(await loadSleevesAction(7)).toEqual([]);
  });

  it("delete: a 204 with no body is a success, not a failure", async () => {
    /* `DELETE` returns no content, so `data` is empty — keying success off `data` would report
       every successful delete as a failure and invite a second one. */
    resetApi(true, undefined);
    const outcome = await deletePortfolioAction(7);

    expect(outcome.ok).toBe(true);
    expect(apiCalls[0]!.method).toBe("DELETE");
  });
});
