import { describe, expect, it } from "vitest";

import type { PortfolioNodeOut, SleeveListOut } from "@baskfy/api-client";

import {
  addMoney,
  classifyInvestment,
  classifySleeve,
  composeBook,
  type BookInvestment,
} from "@/lib/portfolios/book";

function portfolio(
  partial: Partial<PortfolioNodeOut> & { id: number; name: string },
): PortfolioNodeOut {
  return {
    created_at: "2026-01-01T00:00:00Z",
    holdings_count: 0,
    depth: 0,
    ...partial,
  };
}

function investment(partial: Partial<BookInvestment> & { id: string; basket_name: string }): BookInvestment {
  return {
    status: "ACTIVE",
    basket_source: null,
    visibility: null,
    snapshot: {
      money_put_in: "100000",
      current_value: "110000",
      current_returns_pct: "10",
      xirr: "12",
      xirr_displayable: true,
    },
    ...partial,
  };
}

function sleeves(rows: SleeveListOut["sleeves"]): SleeveListOut {
  return {
    sleeves: rows,
    total_capital: addMoney(rows.map((row) => row.capital)) ?? "0",
  };
}

describe("classifyInvestment", () => {
  it("treats a published catalog basket as a manager box", () => {
    expect(classifyInvestment({ visibility: "PUBLISHED", basket_source: "SCREEN" })).toBe(
      "manager",
    );
  });

  it("treats a private screen or scan as a rule you wrote", () => {
    expect(classifyInvestment({ visibility: "PRIVATE", basket_source: "SCREEN" })).toBe("rule");
    expect(classifyInvestment({ visibility: "PRIVATE", basket_source: "SCAN" })).toBe("rule");
  });

  it("treats a private hand-typed list as by hand", () => {
    expect(classifyInvestment({ visibility: "PRIVATE", basket_source: "MANUAL" })).toBe("manual");
  });

  it("defaults an unclassified investment to manager rather than guessing a rule", () => {
    expect(classifyInvestment({})).toBe("manager");
  });
});

describe("classifySleeve", () => {
  it("maps screen sleeves to your rule and manual sleeves to by hand", () => {
    expect(classifySleeve("screen")).toBe("rule");
    expect(classifySleeve("manual")).toBe("manual");
  });
});

describe("composeBook", () => {
  it("puts active investments in their own section, exited ones nowhere", () => {
    const book = composeBook({
      portfolios: [],
      investments: [
        investment({
          id: "1",
          basket_name: "A manager's basket",
          visibility: "PUBLISHED",
          basket_source: "MANUAL",
        }),
        investment({ id: "2", basket_name: "Sold", status: "EXITED" }),
      ],
    });
    expect(book.sections).toHaveLength(1);
    expect(book.sections[0]?.title).toBe("Baskets you hold");
    expect(book.sections[0]?.boxes).toHaveLength(1);
    expect(book.sections[0]?.boxes[0]?.kind).toBe("manager");
    expect(book.sections[0]?.boxes[0]?.currentValue).toBe("110000");
    expect(book.sections[0]?.boxes[0]?.xirr).toBe("12");
    expect(book.totals.boxCount).toBe(1);
    expect(book.totals.basketValue).toBe("110000.00");
    expect(book.totals.sleeveCapital).toBeNull();
  });

  it("hides XIRR when the snapshot says it is not displayable", () => {
    const book = composeBook({
      portfolios: [],
      investments: [
        investment({
          id: "1",
          basket_name: "Young",
          snapshot: {
            money_put_in: "1",
            current_value: "1",
            current_returns_pct: "0",
            xirr: "99",
            xirr_displayable: false,
          },
        }),
      ],
    });
    expect(book.sections[0]?.boxes[0]?.xirr).toBeNull();
  });

  it("turns screen and manual sleeves into separate boxes with assigned capital, not a mark", () => {
    const book = composeBook({
      portfolios: [
        {
          portfolio: portfolio({ id: 9, name: "Main book", holdings_count: 12 }),
          sleeves: sleeves([
            {
              id: 1,
              name: "Momentum rule",
              kind: "screen",
              capital: "500000",
              top_n: 15,
              screen_name: "12-month momentum",
            },
            { id: 2, name: "Long-term", kind: "manual", capital: "6000000", top_n: 15 },
          ]),
        },
      ],
      investments: [],
    });
    expect(book.sections).toHaveLength(1);
    expect(book.sections[0]?.boxes.map((box) => box.kind)).toEqual(["rule", "manual"]);
    expect(book.sections[0]?.boxes.every((box) => box.currentValue === null)).toBe(true);
    expect(book.sections[0]?.boxes.every((box) => box.valueIsLiveMark === false)).toBe(true);
    expect(book.totals.sleeveCapital).toBe("6500000.00");
    expect(book.totals.basketValue).toBeNull();
  });

  it("does not also count CSV holdings as a box when sleeves already split the book", () => {
    const book = composeBook({
      portfolios: [
        {
          portfolio: portfolio({ id: 3, name: "Split", holdings_count: 40 }),
          sleeves: sleeves([
            { id: 1, name: "Rule", kind: "screen", capital: "1", top_n: 15 },
          ]),
        },
      ],
      investments: [],
    });
    expect(book.sections[0]?.boxes).toHaveLength(1);
    expect(book.sections[0]?.boxes[0]?.id).toBe("sleeve-3-1");
  });

  it("shows unsleeved holdings as one by-hand box", () => {
    const book = composeBook({
      portfolios: [
        {
          portfolio: portfolio({ id: 4, name: "Leftovers", holdings_count: 7 }),
          sleeves: sleeves([]),
        },
      ],
      investments: [],
    });
    expect(book.sections[0]?.boxes).toHaveLength(1);
    expect(book.sections[0]?.boxes[0]).toMatchObject({
      kind: "manual",
      holdingsCount: 7,
      capital: null,
      currentValue: null,
    });
  });

  it("keeps an empty named portfolio as a section so it can still be divided or deleted", () => {
    const book = composeBook({
      portfolios: [{ portfolio: portfolio({ id: 5, name: "Empty" }), sleeves: null }],
      investments: [],
    });
    expect(book.sections).toHaveLength(1);
    expect(book.sections[0]?.boxes).toHaveLength(0);
    expect(book.totals.boxCount).toBe(0);
  });

  it("rolls investments and two named books into one overview without summing mark and capital", () => {
    const book = composeBook({
      portfolios: [
        {
          portfolio: portfolio({ id: 1, name: "Trading book" }),
          sleeves: sleeves([
            { id: 1, name: "Rule", kind: "screen", capital: "500000", top_n: 15 },
          ]),
        },
        {
          portfolio: portfolio({ id: 2, name: "Family", holdings_count: 3 }),
          sleeves: sleeves([]),
        },
      ],
      investments: [
        investment({
          id: "8",
          basket_name: "Published momentum",
          visibility: "PUBLISHED",
          snapshot: {
            money_put_in: "3500000",
            current_value: "3600000",
            current_returns_pct: "2.86",
            xirr: null,
            xirr_displayable: false,
          },
        }),
      ],
    });
    expect(book.sections.map((section) => section.title)).toEqual([
      "Baskets you hold",
      "Trading book",
      "Family",
    ]);
    expect(book.totals.boxCount).toBe(3);
    expect(book.totals.basketValue).toBe("3600000.00");
    expect(book.totals.sleeveCapital).toBe("500000.00");
    expect(book.totals.hasLiveMarks).toBe(true);
  });
});

describe("addMoney is exact", () => {
  it("does not drift the way a float sum does", () => {
    // 0.1 + 0.2 === 0.30000000000000004 as doubles; the old implementation went through Number().
    expect(addMoney(["0.10", "0.20"])).toBe("0.30");
  });

  it("keeps every digit at a size a double cannot hold", () => {
    expect(addMoney(["9007199254740992", "1"])).toBe("9007199254740993.00");
  });

  it("keeps more than two places when the inputs carried more", () => {
    expect(addMoney(["1.0001", "2.0002"])).toBe("3.0003");
  });

  it("answers null for nothing summable rather than a made-up zero", () => {
    expect(addMoney([])).toBeNull();
    expect(addMoney([null, ""])).toBeNull();
  });
});

describe("a book nested under another keeps its place", () => {
  it("carries depth and ancestors onto the section", () => {
    const book = composeBook({
      portfolios: [
        { portfolio: portfolio({ id: 1, name: "Everything" }), sleeves: null },
        {
          portfolio: portfolio({ id: 2, name: "Momentum", parent_id: 1, depth: 1 }),
          sleeves: null,
          depth: 1,
          ancestors: ["Everything"],
        },
      ],
      investments: [],
    });
    expect(book.sections.map((section) => section.depth)).toEqual([0, 1]);
    expect(book.sections[1]?.ancestors).toEqual(["Everything"]);
    expect(book.sections[1]?.orphan).toBe(false);
  });

  it("marks an orphaned book as orphaned rather than dropping it", () => {
    const book = composeBook({
      portfolios: [
        {
          portfolio: portfolio({ id: 9, name: "Detached", parent_id: 99, holdings_count: 4 }),
          sleeves: null,
          orphan: true,
        },
      ],
      investments: [],
    });
    expect(book.sections).toHaveLength(1);
    expect(book.sections[0]?.orphan).toBe(true);
  });

  it("still reports a parent's own holdings count and not its subtree's", () => {
    const book = composeBook({
      portfolios: [
        { portfolio: portfolio({ id: 1, name: "Parent", holdings_count: 2 }), sleeves: null },
        {
          portfolio: portfolio({ id: 2, name: "Child", holdings_count: 40, parent_id: 1 }),
          sleeves: null,
          depth: 1,
          ancestors: ["Parent"],
        },
      ],
      investments: [],
    });
    expect(book.sections[0]?.holdingsCount).toBe(2);
    expect(book.sections[1]?.holdingsCount).toBe(40);
  });
});
