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

const { createPortfolioAction } = await import("@/app/actions/portfolio");

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
