import type {
  PortfolioForestOut,
  ScreenDefinition,
  ScreenSelectionOut,
  ScreenSelectionRequest,
} from "@baskfy/api-client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  PortfolioFit,
  PortfolioFitResult,
} from "@/components/screens/portfolio-fit";
import { DISCLAIMER_LABEL } from "@/components/data/disclaimer";
import { defaultDefinition } from "@/lib/screens/defaults";
import { constraintsProblem, EMPTY_CONSTRAINTS } from "@/lib/screens/portfolio-fit";

/**
 * gates/ranking-2.H-web.md G5, part 3 — docs/ranking/PLAN.md C5/C6, `POST /screens/selection`.
 *
 * The spec: a Portfolio fit panel with a portfolio picker (the portfolios API), constraint inputs
 * and a per-row decision with its reasons. It is clearly labelled informational — no orders —
 * and sits apart from the quality ranking, which it never reorders: rows keep the engine's
 * `quality_rank` verbatim, in the server's order. `holdings_without_quantity` is listed where it
 * can be seen. The request is `{definition, as_of?, data_version?, portfolio_id | holdings,
 * constraints}`.
 */

const api = vi.hoisted(() => ({ POST: vi.fn(), GET: vi.fn() }));
vi.mock("@/lib/api/browser", () => ({ browserApi: () => api, accessToken: () => undefined }));

afterEach(cleanup);
beforeEach(() => {
  api.GET.mockReset();
  api.POST.mockReset();
});

const FOREST: PortfolioForestOut = {
  data: [
    {
      id: 7,
      name: "Long term",
      depth: 0,
      holdings_count: 12,
      created_at: "2026-08-01T00:00:00Z",
      children: [
        {
          id: 9,
          name: "Momentum picks",
          depth: 1,
          parent_id: 7,
          holdings_count: 1,
          created_at: "2026-08-02T00:00:00Z",
        },
      ],
    },
  ],
  orphans: [],
};

function ranked(): ScreenDefinition {
  return {
    ...defaultDefinition(),
    sort_by: "ret_6m",
    ranking_terms: [
      { factor: "ret_6m", preference: "higher", weight: 1, target_min: null, target_max: null },
    ],
  };
}

/** Rows in the server's order, which is not quality-rank order. */
const RESULT: ScreenSelectionOut = {
  version: "selection-1",
  as_of: "2026-09-11",
  data_version: 57,
  informational_only: true,
  portfolio_id: 9,
  holdings_without_quantity: ["HDFCBANK", "ITC"],
  constraints: {
    max_names: 15,
    entry_rank: 15,
    retention_rank: 30,
    max_per_sector: 2,
    capital_inr: null,
    max_adv_participation_pct: "1.0",
    turnover_budget_names: null,
    max_correlation: null,
    correlation_window: 126,
  },
  notes: [{ code: "CAPACITY_NOT_CHECKED", message: "No capital given, so size was not checked." }],
  summary: {
    kept: 1,
    entries: 1,
    exits: 1,
    skips: 1,
    sector_counts: { IT: 2 },
    turnover_budget: null,
    turnover_used: 2,
    unfilled_slots: 12,
  },
  provenance: {
    universe: "nifty-500",
    universe_label: "NIFTY 500",
    as_of: "2026-09-11",
    data_version: 57,
    ranking_engine_version: "rank-2.0",
    desk_score_version: null,
    scope: "fixed_universe",
    mode: "composite",
  },
  rows: [
    row({ symbol: "TCS", instrument_id: 3, action: "hold", quality_rank: 22, reasons: ["RANK_WITHIN_RETENTION"], current_quantity: "10" }),
    row({ symbol: "YESBANK", instrument_id: 4, action: "exit", quality_rank: null, reasons: ["NOT_IN_RESULTS"], current_quantity: "100" }),
    row({ symbol: "CUPID", instrument_id: 1, action: "enter", quality_rank: 2, reasons: ["RANK_WITHIN_ENTRY"], flags: ["SECTOR_UNCLASSIFIED"] }),
    row({ symbol: "INFY", instrument_id: 2, action: "skip", quality_rank: 5, reasons: ["SECTOR_CAP"] }),
  ],
};

function row(overrides: Partial<ScreenSelectionOut["rows"][number]>): ScreenSelectionOut["rows"][number] {
  return {
    action: "skip",
    adv_participation_pct: null,
    correlation_peer: null,
    current_quantity: null,
    explanation: "",
    flags: [],
    instrument_id: 0,
    max_correlation: null,
    proposed_qty: null,
    proposed_value_inr: null,
    quality_rank: null,
    reasons: [],
    score: null,
    sector: null,
    symbol: "X",
    ...overrides,
  };
}

function renderFit(definition: ScreenDefinition = ranked()) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <PortfolioFit definition={definition} asOf="2026-09-11" dataVersion={57} />
    </QueryClientProvider>,
  );
}

function lastBody(): ScreenSelectionRequest {
  const call = api.POST.mock.calls.at(-1);
  expect(call?.[0]).toBe("/api/v1/screens/selection");
  return (call?.[1] as { body: ScreenSelectionRequest }).body;
}

describe("PortfolioFit section", () => {
  it("is labelled informational with no orders, carries the disclaimer, and asks nothing until opened", () => {
    renderFit();

    const section = screen.getByTestId("portfolio-fit");
    expect(within(section).getByTestId("fit-informational")).toHaveTextContent(
      "Informational only — no orders",
    );
    expect(within(section).getByLabelText(DISCLAIMER_LABEL)).toBeInTheDocument();
    expect(api.GET).not.toHaveBeenCalled();
    expect(api.POST).not.toHaveBeenCalled();
  });

  it("offers the caller's portfolios, nested ones included, and sends portfolio_id with the set constraints", async () => {
    api.GET.mockResolvedValue({ data: FOREST, error: undefined });
    api.POST.mockResolvedValue({ data: RESULT, error: undefined });
    const user = userEvent.setup();
    const definition = ranked();
    renderFit(definition);

    await user.click(screen.getByTestId("fit-toggle"));
    expect(api.GET).toHaveBeenCalledWith("/api/v1/portfolios");
    const picker = screen.getByTestId("fit-portfolio");
    expect(await within(picker).findByRole("option", { name: /Momentum picks/ })).toBeInTheDocument();
    expect(within(picker).getByRole("option", { name: /Long term/ })).toBeInTheDocument();

    await user.selectOptions(picker, "9");
    fireEvent.change(screen.getByLabelText("Most stocks"), { target: { value: "10" } });
    fireEvent.change(screen.getByLabelText("Most stocks per sector"), { target: { value: "2" } });
    fireEvent.change(screen.getByLabelText("Capital, in rupees"), { target: { value: "500000" } });
    await user.click(screen.getByTestId("fit-check"));

    const body = lastBody();
    expect(body).toEqual({
      definition,
      as_of: "2026-09-11",
      data_version: 57,
      portfolio_id: 9,
      constraints: { max_names: 10, max_per_sector: 2, capital_inr: "500000" },
    });
    expect(body).not.toHaveProperty("holdings");
    expect(typeof body.constraints?.capital_inr).toBe("string");

    expect(await screen.findByTestId("fit-result")).toHaveTextContent("Momentum picks");
    expect(screen.getByTestId("fit-action-CUPID")).toHaveTextContent("Would add");
  });

  it("can start from no holdings, which sends an empty holdings list and no portfolio_id", async () => {
    api.GET.mockResolvedValue({ data: FOREST, error: undefined });
    api.POST.mockResolvedValue({ data: { ...RESULT, portfolio_id: null }, error: undefined });
    const user = userEvent.setup();
    renderFit();

    await user.click(screen.getByTestId("fit-toggle"));
    await user.selectOptions(screen.getByTestId("fit-portfolio"), "empty");
    await user.click(screen.getByTestId("fit-check"));

    const body = lastBody();
    expect(body.holdings).toEqual([]);
    expect(body).not.toHaveProperty("portfolio_id");
    expect(body.constraints).toEqual({});
  });

  it("needs ranking terms: without them the check cannot be sent", async () => {
    api.GET.mockResolvedValue({ data: FOREST, error: undefined });
    const user = userEvent.setup();
    renderFit(defaultDefinition());

    await user.click(screen.getByTestId("fit-toggle"));
    await user.selectOptions(screen.getByTestId("fit-portfolio"), "empty");
    expect(screen.getByTestId("fit-needs-terms")).toBeInTheDocument();
    expect(screen.getByTestId("fit-check")).toBeDisabled();
  });

  it("refuses a keep limit tighter than the add limit before sending", async () => {
    api.GET.mockResolvedValue({ data: FOREST, error: undefined });
    const user = userEvent.setup();
    renderFit();

    await user.click(screen.getByTestId("fit-toggle"));
    await user.selectOptions(screen.getByTestId("fit-portfolio"), "empty");
    fireEvent.change(screen.getByLabelText("Add limit"), { target: { value: "20" } });
    fireEvent.change(screen.getByLabelText("Keep limit"), { target: { value: "10" } });

    expect(screen.getByTestId("fit-constraints-problem")).toHaveTextContent(
      "The keep limit cannot be tighter than the add limit.",
    );
    expect(screen.getByTestId("fit-check")).toBeDisabled();
    expect(api.POST).not.toHaveBeenCalled();
  });
});

describe("constraintsProblem", () => {
  it("accepts blanks, and refuses a non-positive or badly formed capital", () => {
    expect(constraintsProblem(EMPTY_CONSTRAINTS)).toBeNull();
    expect(constraintsProblem({ ...EMPTY_CONSTRAINTS, capital_inr: "0" })).not.toBeNull();
    expect(constraintsProblem({ ...EMPTY_CONSTRAINTS, capital_inr: "1e6" })).not.toBeNull();
    expect(constraintsProblem({ ...EMPTY_CONSTRAINTS, capital_inr: "250000.50" })).toBeNull();
    expect(constraintsProblem({ ...EMPTY_CONSTRAINTS, max_correlation: 1.5 })).not.toBeNull();
    expect(constraintsProblem({ ...EMPTY_CONSTRAINTS, max_names: 2.5 })).not.toBeNull();
  });
});

describe("PortfolioFitResult", () => {
  it("shows a decision and plain reasons per row, in the server's order, with quality rank verbatim", () => {
    render(<PortfolioFitResult result={RESULT} portfolioName="Momentum picks" />);

    const symbols = within(screen.getByTestId("fit-rows"))
      .getAllByRole("rowheader")
      .map((cell) => cell.textContent);
    expect(symbols).toEqual(["TCS", "YESBANK", "CUPID", "INFY"]);

    expect(screen.getByTestId("fit-rank-TCS")).toHaveTextContent("#22");
    expect(screen.getByTestId("fit-rank-YESBANK")).toHaveTextContent("—");
    expect(screen.getByTestId("fit-rank-CUPID")).toHaveTextContent("#2");

    expect(screen.getByTestId("fit-action-TCS")).toHaveTextContent("Keep");
    expect(screen.getByTestId("fit-action-YESBANK")).toHaveTextContent("Would remove");
    expect(screen.getByTestId("fit-action-CUPID")).toHaveTextContent("Would add");
    expect(screen.getByTestId("fit-action-INFY")).toHaveTextContent("Skipped");

    expect(screen.getByTestId("fit-reasons-YESBANK")).toHaveTextContent(
      "Not in this screen's results",
    );
    expect(screen.getByTestId("fit-reasons-INFY")).toHaveTextContent(
      "Its sector is already at the limit",
    );
    expect(screen.getByTestId("fit-flags-CUPID")).toHaveTextContent("No sector on record");
  });

  it("lists holdings without a quantity where they can be seen", () => {
    render(<PortfolioFitResult result={RESULT} />);

    const box = screen.getByTestId("fit-holdings-without-quantity");
    expect(box).toBeVisible();
    expect(box).toHaveTextContent("HDFCBANK");
    expect(box).toHaveTextContent("ITC");
    expect(box).toHaveTextContent("(2)");
  });

  it("shows the summary, notes and the run's provenance", () => {
    render(<PortfolioFitResult result={RESULT} />);

    expect(screen.getByTestId("fit-summary-kept")).toHaveTextContent("1");
    expect(screen.getByTestId("fit-summary-unfilled")).toHaveTextContent("12");
    expect(screen.getByTestId("fit-notes")).toHaveTextContent("No capital given");
    expect(screen.getByTestId("provenance-engine")).toHaveTextContent("rank-2.0");
    expect(screen.getByTestId("provenance-as_of")).toBeInTheDocument();
  });

  it("uses no trading words that could read as placing an order", () => {
    const withSize: ScreenSelectionOut = {
      ...RESULT,
      rows: RESULT.rows.map((r) =>
        r.symbol === "CUPID" ? { ...r, proposed_qty: 12, proposed_value_inr: "24000.00" } : r,
      ),
    };
    const { container } = render(<PortfolioFitResult result={withSize} />);

    expect(screen.getByText(/12 shares/)).toHaveTextContent("₹24,000");
    expect(container.textContent).not.toMatch(/\b(buy|sell|place order|execute|submit order)\b/i);
  });
});
