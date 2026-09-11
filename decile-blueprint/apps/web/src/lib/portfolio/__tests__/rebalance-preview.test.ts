import { describe, expect, it } from "vitest";

import {
  IDS,
  SCREENS,
  detail,
  rebalance,
} from "@/components/portfolio/rebalance/__tests__/fixtures";
import {
  NOT_PRODUCED_HERE,
  WORKFLOW,
  planText,
  rebalancePreview,
  stepBlockedReason,
  type PreviewInput,
} from "@/lib/portfolio/rebalance-preview";

/**
 * The rebalance preview's arithmetic and its refusals.
 *
 * These assert the *spec* — what the drawer is for — rather than what the code happens to do:
 * that a weight is exact, that a missing weight never becomes a zero, that an exclusion is not
 * re-spread over the names that remain, and that no quantity, cash figure, turnover or cost is
 * ever computed from the payload. The last one is the reason this leaf exists: a derived quantity
 * beside a symbol reads as an instruction, and Baskfy places real orders elsewhere.
 */

function input(over: Partial<PreviewInput> = {}): PreviewInput {
  return {
    portfolioName: "Momentum 20",
    rebalance: rebalance(),
    detail: detail(),
    screens: SCREENS,
    excluded: new Set<number>(),
    ...over,
  };
}

function rowFor(preview: ReturnType<typeof rebalancePreview>, symbol: string) {
  const row = preview.rows.find((candidate) => candidate.symbol === symbol);
  if (row === undefined) throw new Error(`no row for ${symbol}`);
  return row;
}

describe("current against target weights", () => {
  it("carries both weights for every name on all four lists", () => {
    const preview = rebalancePreview(input());

    expect(preview.status).toBe("ready");
    expect(preview.lists.map((list) => [list.key, list.count])).toEqual([
      ["exit", 2],
      ["entry", 3],
      ["inside-window", 1],
      ["hold", 2],
    ]);
    /* Every held name and every entrant appears exactly once. */
    expect(preview.rows).toHaveLength(8);
    expect(new Set(preview.rows.map((row) => row.symbol)).size).toBe(8);
  });

  it("sums a name held at two brokers into one weight, and names both accounts", () => {
    const infy = rowFor(rebalancePreview(input()), "INFY");

    /* 0.15 at Zerodha + 0.10 at Upstox. Added as scaled integers, never through a float. */
    expect(infy.current.value).toBe("25.00");
    expect(infy.brokers).toEqual(["Upstox", "Zerodha"]);
  });

  it("computes the change from the fractions, not from two rounded percentages", () => {
    const tcs = rowFor(rebalancePreview(input()), "TCS");

    expect(tcs.current.value).toBe("30.00");
    expect(tcs.target.value).toBe("16.67");
    /* 0.166666 - 0.300000 = -0.133334 -> -13.3334 -> -13.33. Subtracting the displayed values
       would give -13.33 here too, but the coverage assertion below is where it shows. */
    expect(tcs.delta.value).toBe("-13.33");
  });

  it("target weights that are equal to six decimals still add to exactly 100%", () => {
    const preview = rebalancePreview(input());

    /* Six weights of 16.6666% displayed at two decimals would add to 100.02. The sum is done on
       the fractions and converted once. */
    expect(preview.impact.coverage.value).toBe("100.00");
    expect(preview.impact.leftUnassigned.value).toBe("0.00");
  });

  it("an entry's current weight is a measured zero; an unpriced holding's is a reason", () => {
    const preview = rebalancePreview(input());

    /* Not held is genuinely zero — the server has just established it. */
    expect(rowFor(preview, "HDFCBANK").current.value).toBe("0.00");
    expect(rowFor(preview, "HDFCBANK").target.value).toBe("16.67");

    /* No price is not zero. House rule: absent is not zero. */
    const suzlon = rowFor(preview, "SUZLON");
    expect(suzlon.current.value).toBeNull();
    expect(suzlon.current.unavailable).toBe("No price today, so it has no weight.");
    expect(suzlon.delta.value).toBeNull();
    expect(suzlon.delta.unavailable).toContain("No price today");
  });

  it("an exit's target weight is zero, because the target set is the names that stay", () => {
    const dhfl = rowFor(rebalancePreview(input()), "DHFL");

    expect(dhfl.current.value).toBe("25.00");
    expect(dhfl.target.value).toBe("0.00");
    expect(dhfl.delta.value).toBe("-25.00");
  });

  it("says why there is no current weight when the holdings were never loaded", () => {
    const preview = rebalancePreview(input({ detail: null }));
    const tcs = rowFor(preview, "TCS");

    expect(tcs.current.value).toBeNull();
    expect(tcs.current.unavailable).toBe("Holdings not loaded, so there is no weight to compare.");
    /* Never a bare null with no explanation, anywhere. */
    for (const row of preview.rows) {
      for (const figure of [row.current, row.target, row.delta]) {
        if (figure.value === null) expect(figure.unavailable).toBeTruthy();
      }
    }
  });
});

describe("nothing is derived", () => {
  it("produces no quantity, cash, turnover or cost field at all", () => {
    const preview = rebalancePreview(input());
    const serialised = JSON.stringify(preview);

    /* Not "renders them as unavailable" — there is no such field on the shape. A figure that does
       not exist cannot be shown by accident. */
    expect(serialised).not.toMatch(/"quantity"/);
    expect(serialised).not.toMatch(/"planned_qty"/);
    expect(serialised).not.toMatch(/"cash"/);
    expect(serialised).not.toMatch(/"turnover"/);
    expect(serialised).not.toMatch(/"brokerage"|"charges"|"cost"/);
  });

  it("names every blocked figure with a reason and where it comes from instead", () => {
    for (const item of NOT_PRODUCED_HERE) {
      expect(item.name.length).toBeGreaterThan(3);
      expect(item.reason.length).toBeGreaterThan(20);
      expect(item.insteadFrom.length).toBeGreaterThan(10);
      /* "Coming soon" is not something a person can act on. */
      expect(item.insteadFrom.toLowerCase()).not.toContain("coming soon");
    }
    expect(NOT_PRODUCED_HERE.map((item) => item.id)).toEqual(
      expect.arrayContaining(["quantity", "cash", "turnover", "costs", "tax-lots"]),
    );
  });

  it("counts name churn and refuses to call it turnover", () => {
    const impact = rebalancePreview(input()).impact;

    expect(impact.entering).toBe(3);
    expect(impact.exiting).toBe(2);
    expect(impact.staying).toBe(3);
    /* Names, not value. The distinction is the whole reason `turnover` is on the blocked list. */
    expect(Object.keys(impact)).not.toContain("turnover");
  });
});

describe("exclusions", () => {
  it("an excluded entry lowers coverage and leaves the slice unassigned", () => {
    const preview = rebalancePreview(input({ excluded: new Set([IDS.HDFCBANK]) }));

    expect(preview.impact.coverage.value).toBe("83.33");
    expect(preview.impact.leftUnassigned.value).toBe("16.67");
    /* The other five keep the weights the server computed. Re-spreading 16.67% across them would
       be this preview inventing an allocation the screen never proposed. */
    expect(rowFor(preview, "TCS").target.value).toBe("16.67");
    expect(preview.impact.namesAfter).toBe(5);
    expect(preview.impact.entering).toBe(2);
  });

  it("keeping a name the screen exits makes the after-weights indeterminate, and says so", () => {
    const preview = rebalancePreview(input({ excluded: new Set([IDS.DHFL]) }));

    expect(preview.impact.largestAfter.value).toBeNull();
    expect(preview.impact.largestAfter.unavailable).toContain("DHFL");
    expect(preview.impact.largestAfter.unavailable).toContain("no longer add to 100%");
    expect(preview.impact.topFiveAfter.value).toBeNull();
    /* The name is still in the book, so the count goes up rather than down. */
    expect(preview.impact.namesAfter).toBe(7);
    expect(preview.impact.exiting).toBe(1);
  });

  it("only an entry or an exit can be excluded", () => {
    const preview = rebalancePreview(input());

    for (const row of preview.rows) {
      expect(row.excludable).toBe(row.side === "entry" || row.side === "exit");
    }
  });

  it("is reversible: the empty set restores every figure exactly", () => {
    const before = rebalancePreview(input());
    const after = rebalancePreview(input({ excluded: new Set<number>() }));

    expect(after).toEqual(before);
  });
});

describe("concentration before and after", () => {
  it("measures the largest and top-five weights on both sides", () => {
    const impact = rebalancePreview(input()).impact;

    expect(impact.largestNow.value).toBe("30.00");
    expect(impact.largestAfter.value).toBe("16.67");
    expect(impact.topFiveNow.value).toBe("100.00");
    expect(impact.topFiveAfter.value).toBe("83.33");
    expect(impact.concentrationMove).toBe("less-concentrated");
  });

  it("reports an unknown holding count as unknown rather than as zero", () => {
    const impact = rebalancePreview(input({ detail: null })).impact;

    expect(impact.namesNow).toBeNull();
    expect(impact.largestNow.value).toBeNull();
    expect(impact.largestNow.unavailable).toBe(
      "Holdings not loaded, so there is no weight to compare.",
    );
    /* The target side is still knowable — it comes from the proposal, not from the book. */
    expect(impact.namesAfter).toBe(6);
    expect(impact.largestAfter.value).toBe("16.67");
  });

  it("flags that the current side covers priced holdings only", () => {
    expect(rebalancePreview(input()).impact.pricedOnly).toBe(true);

    const allPriced = detail();
    const priced = {
      ...allPriced,
      holdings: (allPriced.holdings ?? []).filter((holding) => holding.weight !== null),
    };
    expect(rebalancePreview(input({ detail: priced })).impact.pricedOnly).toBe(false);
  });
});

describe("warnings", () => {
  it("names the delisted holding and the unpriced one rather than counting them", () => {
    const warnings = rebalancePreview(input()).warnings;
    const byId = new Map(warnings.map((warning) => [warning.id, warning]));

    expect(byId.get("delisted")?.headline).toContain("DHFL");
    expect(byId.get("delisted")?.level).toBe("critical");
    expect(byId.get("unpriced")?.headline).toContain("SUZLON");
  });

  it("warns when the screen ran on an older session than the holdings are marked at", () => {
    const preview = rebalancePreview(
      input({ detail: detail({ prices_as_of: "2026-09-11" }) }),
    );
    const stale = preview.warnings.find((warning) => warning.id === "stale-as-of");

    expect(stale?.headline).toContain("2026-09-10");
    expect(stale?.headline).toContain("2026-09-11");
  });

  it("warns when the requested date is not the date the diff could use", () => {
    const preview = rebalancePreview(
      input({ rebalance: rebalance({ requested_as_of: "2026-09-11" }) }),
    );

    expect(preview.warnings.find((warning) => warning.id === "requested-as-of")?.headline).toContain(
      "2026-09-11",
    );
  });

  it("warns when the screen returned fewer names than the top N asked for", () => {
    const preview = rebalancePreview(
      input({ rebalance: rebalance({ screen_result_count: 3 }) }),
    );

    expect(preview.warnings.find((warning) => warning.id === "screen-short")?.headline).toContain(
      "3 names",
    );
  });

  it("warns when the diff and the portfolio on screen are not the same book", () => {
    const preview = rebalancePreview(input({ rebalance: rebalance({ holdings_count: 9 }) }));

    expect(preview.warnings.find((warning) => warning.id === "holdings-drift")?.level).toBe(
      "critical",
    );
  });

  it("names the unreconciled holding", () => {
    const withDrift = detail({ pending_reconciliation: true });
    const preview = rebalancePreview(input({ detail: withDrift }));

    expect(preview.warnings.find((warning) => warning.id === "reconciliation")).toBeDefined();
  });
});

describe("states and the workflow", () => {
  it("a portfolio with no screen behind it is blocked, with exactly one next action", () => {
    const preview = rebalancePreview(input({ screens: [] }));

    expect(preview.status).toBe("no-screen");
    expect(preview.blocker?.action).toEqual({ label: "Build a screen", href: "/build" });
    expect(preview.rows).toHaveLength(0);
    expect(preview.blocker?.detail).toContain("no target allocation stored");
  });

  it("before the first diff there is nothing to review", () => {
    const preview = rebalancePreview(input({ rebalance: null }));

    expect(preview.status).toBe("not-analysed");
    for (const step of ["adjust", "impact", "confirm", "plan"] as const) {
      expect(stepBlockedReason(step, preview, false)).toContain("Run the diff first");
    }
    expect(stepBlockedReason("analyse", preview, false)).toBeNull();
  });

  it("the plan step waits for the acknowledgement", () => {
    const preview = rebalancePreview(input());

    expect(stepBlockedReason("plan", preview, false)).toContain("Acknowledge");
    expect(stepBlockedReason("plan", preview, true)).toBeNull();
    expect(WORKFLOW.map((step) => step.key)).toEqual([
      "analyse",
      "adjust",
      "impact",
      "confirm",
      "plan",
    ]);
  });
});

describe("the plan", () => {
  it("is a document with no quantity in it, and says whose job the quantities are", () => {
    const text = planText({ preview: rebalancePreview(input()), notes: new Map() });

    expect(text).toContain("A PLAN, NOT AN ORDER");
    expect(text).toContain("Baskfy has not sent any of this to a broker");
    expect(text).toContain("your broker's own order window");
    /* No rupee figure anywhere: nothing in this drawer is money. */
    expect(text).not.toContain("₹");
  });

  it("carries the reader's own notes and the names they excluded", () => {
    const preview = rebalancePreview(input({ excluded: new Set([IDS.ITC]) }));
    const text = planText({
      preview,
      notes: new Map([[IDS.ITC, "waiting for results"]]),
    });

    expect(text).toContain("EXCLUDED BY YOU (1)");
    expect(text).toContain("waiting for results");
    /* An excluded name is not in the ENTER section. */
    const enterSection = text.slice(text.indexOf("ENTER"), text.indexOf("HOLD"));
    expect(enterSection).not.toContain("ITC");
  });

  it("lists every figure it does not carry, with where each comes from", () => {
    const text = planText({ preview: rebalancePreview(input()), notes: new Map() });

    for (const item of NOT_PRODUCED_HERE) expect(text).toContain(item.name);
    expect(text).toContain("Baskfy is not a registered investment adviser");
  });

  it("says so plainly when there is no diff to write a plan from", () => {
    const text = planText({
      preview: rebalancePreview(input({ rebalance: null })),
      notes: new Map(),
    });

    expect(text).toContain("No diff has been run");
  });
});
