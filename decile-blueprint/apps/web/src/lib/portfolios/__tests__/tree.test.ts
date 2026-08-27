import type { PortfolioForestOut, PortfolioNodeOut, PortfolioRollupOut } from "@baskfy/api-client";
import { describe, expect, it } from "vitest";

import {
  attributionFor,
  brokerNamesFrom,
  describeOwnHoldings,
  flattenNodes,
  forestRows,
  parentChoices,
  spanningNodeIds,
  subtreeIds,
} from "@/lib/portfolios/tree";

function node(partial: Partial<PortfolioNodeOut> & { id: number; name: string }): PortfolioNodeOut {
  return {
    created_at: "2026-01-01T00:00:00Z",
    holdings_count: 0,
    depth: 0,
    ...partial,
  };
}

function rollup(partial: Partial<PortfolioRollupOut> & { portfolio_id: number }): PortfolioRollupOut {
  return {
    by_broker: [],
    declaration_conflicts: false,
    rows: [],
    spans_brokers: false,
    subtree_portfolio_ids: [partial.portfolio_id],
    total: { cost: "0", holdings: 0, quantity: "0" },
    unattributed: { cost: "0", holdings: 0, quantity: "0" },
    ...partial,
  };
}

const FOREST: PortfolioForestOut = {
  data: [
    node({
      id: 1,
      name: "Everything",
      depth: 0,
      holdings_count: 2,
      children: [
        node({
          id: 2,
          name: "Momentum",
          depth: 1,
          parent_id: 1,
          holdings_count: 12,
          broker_account_id: 7,
        }),
        node({
          id: 3,
          name: "Family",
          depth: 1,
          parent_id: 1,
          holdings_count: 0,
          children: [node({ id: 4, name: "Spouse", depth: 2, parent_id: 3, holdings_count: 5 })],
        }),
      ],
    }),
  ],
  orphans: [node({ id: 9, name: "Detached", depth: 3, parent_id: 99, holdings_count: 4 })],
};

describe("the forest flattens into rows that keep their place", () => {
  it("walks depth-first, parents before children", () => {
    const { rows } = forestRows(FOREST);
    expect(rows.map((row) => row.node.id)).toEqual([1, 2, 3, 4]);
    expect(rows.map((row) => row.depth)).toEqual([0, 1, 1, 2]);
  });

  it("carries the ancestors so a nested row can say where it lives", () => {
    const { rows } = forestRows(FOREST);
    expect(rows[3]?.ancestors).toEqual(["Everything", "Family"]);
    expect(rows[0]?.ancestors).toEqual([]);
  });

  it("counts every portfolio in the forest, orphans included", () => {
    const { total, deepest, orphanRows } = forestRows(FOREST);
    expect(total).toBe(5);
    expect(deepest).toBe(2);
    expect(orphanRows).toHaveLength(1);
  });

  it("reports an orphan rather than dropping it", () => {
    const { rows, orphanRows } = forestRows(FOREST);
    expect(rows.map((row) => row.node.id)).not.toContain(9);
    expect(orphanRows[0]?.orphan).toBe(true);
    // Rendered at the top level, whatever depth the server computed against a parent we cannot see.
    expect(orphanRows[0]?.depth).toBe(0);
    expect(orphanRows[0]?.node.depth).toBe(3);
  });

  it("survives an empty or absent forest without inventing rows", () => {
    expect(forestRows(null).total).toBe(0);
    expect(forestRows({ data: [] }).total).toBe(0);
    expect(flattenNodes([])).toEqual([]);
  });
});

describe("broker attribution never invents a single broker", () => {
  it("names the account when the portfolio declares one", () => {
    const names = brokerNamesFrom([
      rollup({
        portfolio_id: 1,
        by_broker: [
          {
            broker_account_id: 7,
            broker_id: "zerodha",
            label: "Zerodha · main",
            totals: { cost: "1", holdings: 1, quantity: "1" },
          },
        ],
      }),
    ]);
    const attribution = attributionFor(node({ id: 2, name: "Momentum", broker_account_id: 7 }), {
      names,
    });
    expect(attribution.kind).toBe("account");
    expect(attribution.label).toBe("Zerodha · main");
  });

  it("falls back to the account number rather than guessing a broker name", () => {
    const attribution = attributionFor(node({ id: 2, name: "Momentum", broker_account_id: 7 }));
    expect(attribution).toMatchObject({ kind: "account", label: "Broker account 7" });
  });

  it("says how many brokers a roll-up node spans when the roll-up has been read", () => {
    const attribution = attributionFor(node({ id: 1, name: "Everything", broker_account_id: null }), {
      rollup: rollup({
        portfolio_id: 1,
        spans_brokers: true,
        by_broker: [
          {
            broker_account_id: 7,
            broker_id: "zerodha",
            label: "Zerodha · main",
            totals: { cost: "1", holdings: 1, quantity: "1" },
          },
          {
            broker_account_id: 8,
            broker_id: "zerodha",
            label: "Zerodha · family",
            totals: { cost: "1", holdings: 1, quantity: "1" },
          },
        ],
      }),
    });
    expect(attribution).toEqual({
      kind: "spans",
      brokerCount: 2,
      label: "Connected to 2 brokers",
    });
  });

  it("says only that it spans when no roll-up has been read, never a broker name", () => {
    const attribution = attributionFor(node({ id: 1, name: "Everything" }));
    expect(attribution).toEqual({
      kind: "spans",
      brokerCount: null,
      label: "Connected to your brokers",
    });
    expect(attribution.label).not.toMatch(/zerodha/i);
  });

  it("distinguishes a container that spans nothing yet from one that was never asked", () => {
    const attribution = attributionFor(node({ id: 1, name: "Empty" }), {
      rollup: rollup({ portfolio_id: 1 }),
    });
    expect(attribution).toMatchObject({ kind: "spans", brokerCount: 0 });
    expect(attribution.label).toMatch(/nothing here yet/);
  });

  it("asks the server only about the containers that declare no broker account", () => {
    expect(spanningNodeIds(FOREST)).toEqual([1, 3, 4, 9]);
  });
});

describe("the holdings count says which question it answered", () => {
  it("calls a leaf's count its own", () => {
    const { rows } = forestRows(FOREST);
    expect(describeOwnHoldings(rows[1]!)).toBe("12 holdings filed here");
  });

  it("warns that a parent's count is not its subtree's", () => {
    const { rows } = forestRows(FOREST);
    const parent = rows[0];
    expect(parent).toBeDefined();
    expect(describeOwnHoldings(parent!)).toMatch(/counted separately/);
    // The parent holds 2 of the forest's 23 rows; nothing here adds the children into it.
    expect(describeOwnHoldings(parent!)).toMatch(/^2 holdings/);
  });

  it("gets the singular right", () => {
    const row = flattenNodes([node({ id: 1, name: "One", holdings_count: 1 })])[0];
    expect(row).toBeDefined();
    expect(describeOwnHoldings(row!)).toBe("1 holding filed here");
  });
});

describe("a move is only offered where it could legally land", () => {
  it("never offers a portfolio itself or anything beneath it", () => {
    const { rows } = forestRows(FOREST);
    const family = rows.find((row) => row.node.name === "Family");
    expect(family).toBeDefined();
    expect(subtreeIds(family!.node)).toEqual([3, 4]);
    expect(parentChoices(rows, family!.node).map((choice) => choice.id)).toEqual([1, 2]);
  });

  it("offers every other portfolio, indented so the option list reads as the tree", () => {
    const { rows } = forestRows(FOREST);
    const momentum = rows.find((row) => row.node.name === "Momentum");
    expect(parentChoices(rows, momentum!.node)).toEqual([
      { id: 1, label: "Everything" },
      { id: 3, label: "— Family" },
      { id: 4, label: "— — Spouse" },
    ]);
  });

  it("offers a leaf every other row, since nothing sits beneath it", () => {
    const { rows } = forestRows(FOREST);
    const spouse = rows.find((row) => row.node.name === "Spouse");
    expect(subtreeIds(spouse!.node)).toEqual([4]);
    expect(parentChoices(rows, spouse!.node)).toHaveLength(3);
  });
});
