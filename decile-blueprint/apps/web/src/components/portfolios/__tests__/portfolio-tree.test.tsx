import type { PortfolioForestOut, PortfolioNodeOut, PortfolioRollupOut } from "@baskfy/api-client";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { PortfolioTree } from "@/components/portfolios/portfolio-tree";

function node(partial: Partial<PortfolioNodeOut> & { id: number; name: string }): PortfolioNodeOut {
  return { created_at: "2026-01-01T00:00:00Z", holdings_count: 0, depth: 0, ...partial };
}

function rollup(
  portfolioId: number,
  accounts: { id: number; label: string }[],
): PortfolioRollupOut {
  return {
    portfolio_id: portfolioId,
    by_broker: accounts.map((account) => ({
      broker_account_id: account.id,
      broker_id: "zerodha",
      label: account.label,
      totals: { cost: "1", quantity: "1", holdings: 1 },
    })),
    declaration_conflicts: false,
    rows: [],
    spans_brokers: accounts.length > 1,
    subtree_portfolio_ids: [portfolioId],
    total: { cost: "1", quantity: "1", holdings: 1 },
    unattributed: { cost: "0", quantity: "0", holdings: 0 },
  };
}

const FOREST: PortfolioForestOut = {
  data: [
    node({
      id: 1,
      name: "Everything",
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
      ],
    }),
  ],
  orphans: [],
};

describe("the portfolio list renders nesting rather than a flat row", () => {
  it("puts a child under its parent, indented", () => {
    render(<PortfolioTree forest={FOREST} />);
    const rows = screen.getAllByTestId("portfolio-tree-row");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveAttribute("data-portfolio-id", "1");
    expect(rows[0]).toHaveAttribute("data-depth", "0");
    expect(rows[1]).toHaveAttribute("data-portfolio-id", "2");
    expect(rows[1]).toHaveAttribute("data-depth", "1");
    // The style attribute rather than the computed style: jsdom's getComputedStyle does not
    // resolve `rem`, so `toHaveStyle` would compare against an empty string and pass on nothing.
    expect(rows[1]?.getAttribute("style")).toBe("margin-left: 1.25rem;");
    expect(rows[0]?.getAttribute("style")).toBe("margin-left: 0rem;");
  });

  it("names the parent on the nested row so its place is readable, not only visual", () => {
    render(<PortfolioTree forest={FOREST} />);
    expect(screen.getByText("under Everything")).toBeInTheDocument();
  });

  it("says a parent's holdings count is its own and not its subtree's", () => {
    render(<PortfolioTree forest={FOREST} />);
    expect(screen.getByText(/2 holdings filed here · portfolios under this one/)).toBeInTheDocument();
    expect(screen.getByText("12 holdings filed here")).toBeInTheDocument();
  });

  it("renders nothing at all rather than an empty frame when there are no portfolios", () => {
    const { container } = render(<PortfolioTree forest={{ data: [] }} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("every row says which broker account it is attributed to", () => {
  it("names the broker account a portfolio declares", () => {
    render(
      <PortfolioTree
        forest={FOREST}
        rollups={new Map([[1, rollup(1, [{ id: 7, label: "Zerodha · main" }])]])}
      />,
    );
    const child = screen.getAllByTestId("portfolio-tree-row")[1];
    expect(child).toBeDefined();
    const badge = within(child!).getByTestId("broker-attribution");
    expect(badge).toHaveAttribute("data-attribution", "account");
    expect(badge).toHaveTextContent("Zerodha · main");
  });

  it("says a roll-up node spans N brokers rather than showing one false broker", () => {
    render(
      <PortfolioTree
        forest={FOREST}
        rollups={new Map([
          [
            1,
            rollup(1, [
              { id: 7, label: "Zerodha · main" },
              { id: 8, label: "Zerodha · family" },
            ]),
          ],
        ])}
      />,
    );
    const root = screen.getAllByTestId("portfolio-tree-row")[0];
    expect(root).toBeDefined();
    const badge = within(root!).getByTestId("broker-attribution");
    expect(badge).toHaveAttribute("data-attribution", "spans");
    expect(badge).toHaveAttribute("data-broker-count", "2");
    expect(badge).toHaveTextContent("Spans 2 brokers");
    expect(badge).not.toHaveTextContent("Zerodha · main");
  });

  it("says only that it spans when the split has not been read, never a single broker", () => {
    render(<PortfolioTree forest={FOREST} />);
    const root = screen.getAllByTestId("portfolio-tree-row")[0];
    expect(root).toBeDefined();
    expect(within(root!).getByTestId("broker-attribution")).toHaveTextContent("Spans brokers");
  });

  it("offers every row a way through to the per-broker split", () => {
    render(<PortfolioTree forest={FOREST} />);
    const links = screen.getAllByRole("link", { name: "Whose money is where" });
    expect(links).toHaveLength(2);
    expect(links[0]).toHaveAttribute("href", "/portfolios/1/brokers");
  });
});

describe("an orphaned fragment is reported rather than dropped", () => {
  const damaged: PortfolioForestOut = {
    data: FOREST.data,
    orphans: [node({ id: 9, name: "Detached", depth: 3, parent_id: 99, holdings_count: 4 })],
  };

  it("lists it in its own block and says what it means", () => {
    render(<PortfolioTree forest={damaged} />);
    const block = screen.getByTestId("portfolio-orphans");
    expect(within(block).getByText("Detached")).toBeInTheDocument();
    expect(block).toHaveTextContent(/names a parent portfolio that is not yours/);
    expect(block).toHaveTextContent(/sign of damage/);
  });

  it("counts it in the summary, so the totals cannot silently disagree with the list", () => {
    render(<PortfolioTree forest={damaged} />);
    expect(screen.getByTestId("portfolio-tree-summary")).toHaveTextContent("3 portfolios");
  });

  it("shows no orphan block when nothing is detached", () => {
    render(<PortfolioTree forest={FOREST} />);
    expect(screen.queryByTestId("portfolio-orphans")).not.toBeInTheDocument();
  });
});

describe("the tree is where nesting is made, and it can express both moves", () => {
  it("offers no move control at all when the caller supplied no handler", () => {
    render(<PortfolioTree forest={FOREST} />);
    expect(screen.queryByTestId("move-portfolio")).not.toBeInTheDocument();
  });

  it("files a portfolio under a parent by sending that parent's id", () => {
    const onMove = vi.fn();
    render(<PortfolioTree forest={FOREST} onMove={onMove} />);
    const control = screen.getByLabelText("File Momentum under");
    fireEvent.change(control, { target: { value: "1" } });
    expect(onMove).toHaveBeenCalledWith({ id: 2, parentId: 1 });
  });

  it("promotes to a root by sending an explicit null, not by omitting the field", () => {
    const onMove = vi.fn();
    render(<PortfolioTree forest={FOREST} onMove={onMove} />);
    fireEvent.change(screen.getByLabelText("File Momentum under"), { target: { value: "" } });
    const call: unknown = onMove.mock.calls[0]?.[0];
    expect(call).toEqual({ id: 2, parentId: null });
    expect(Object.keys(call as object)).toContain("parentId");
  });

  it("does not offer a portfolio itself, or its own children, as its parent", () => {
    render(<PortfolioTree forest={FOREST} onMove={vi.fn()} />);
    // "Everything" is the root and "Momentum" is its child, so both are inside its own subtree:
    // top level is the only place it could go, and re-parenting under its own child — the cycle
    // the server refuses — is never offered.
    const root = within(screen.getByLabelText("File Everything under"))
      .getAllByRole("option")
      .map((option) => option.textContent);
    expect(root).toEqual(["Top level"]);

    // The child, which has nothing beneath it, may go under the root or up to the top level.
    const child = within(screen.getByLabelText("File Momentum under"))
      .getAllByRole("option")
      .map((option) => option.textContent);
    expect(child).toEqual(["Top level", "Everything"]);
  });

  it("disables the control while a move is in flight", () => {
    render(<PortfolioTree forest={FOREST} onMove={vi.fn()} moving />);
    expect(screen.getByLabelText("File Momentum under")).toBeDisabled();
  });
});
