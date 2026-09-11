import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { UnallocatedSection } from "@/components/portfolio/unallocated-section";
import type {
  AggregatedHolding,
  GroupingSuggestion,
  HoldingBrokerLine,
  Unallocated,
} from "@/lib/portfolio/organize";

/**
 * PORTFOLIO_REDESIGN.md §6.6 and §6.7, and acceptance criterion 8, asserted as behaviour.
 *
 * These tests are written against the spec's sentences, not against the markup: the em dash for a
 * missing figure, the "HDFC Bank — 320 (Zerodha 200 · Upstox 120)" line, the absence of any
 * quantity control anywhere in the picker (§4.2), and the one thing the empty state is allowed to
 * offer a user with no broker connected.
 */

function leg(
  brokerAccountId: number,
  label: string,
  quantity: string,
  value: string | null,
): HoldingBrokerLine {
  return {
    broker: { broker_account_id: brokerAccountId, broker_id: label.toLowerCase(), label },
    quantity,
    // 0035: nothing is filed away in these fixtures, so every share is free. A leg's free
    // quantity is what the picker may offer, and defaulting it to the whole position keeps these
    // tests describing the same pre-split world they were written for.
    unallocated_quantity: quantity,
    value,
    price: null,
    avg_price: null,
    cost_basis: null,
    allocation: null,
    monitoring_views: [],
    first_bought_on: null,
    history_source: "NONE",
    pending_reconciliation: false,
  };
}

function holding(
  instrumentId: number,
  symbol: string,
  name: string,
  legs: HoldingBrokerLine[],
  value: string | null,
  allocated = false,
): AggregatedHolding {
  return {
    instrument: { instrument_id: instrumentId, symbol, name },
    quantity: legs.reduce((sum, line) => sum + Number(line.quantity), 0).toString(),
    price: null,
    price_as_of: null,
    value,
    allocation: null,
    allocated,
    split_across_portfolios: false,
    monitoring_views: [],
    brokers: legs,
    pending_reconciliation: false,
  };
}

const HDFC = holding(
  1,
  "HDFCBANK",
  "HDFC Bank",
  [leg(7, "Zerodha", "200", "300000.00"), leg(8, "Upstox", "120", "180000.00")],
  "480000.00",
);
const ITC = holding(2, "ITC", "ITC", [leg(7, "Zerodha", "500", "225000.50")], "225000.50");

const ROWS = [HDFC, ITC];

const UNALLOCATED: Unallocated = {
  cash: "125000.00",
  cash_by_broker: [
    {
      broker: { broker_account_id: 7, broker_id: "zerodha", label: "Zerodha" },
      balance: "125000.00",
      as_of: "2026-08-25",
    },
  ],
  holdings: [],
  holdings_value: "705000.50",
  holdings_count: 3,
  total_value: "830000.50",
  pending_reconciliation: false,
  cta: "Organize into portfolios",
};

const EMPTY_UNALLOCATED: Unallocated = {
  cash: "0",
  cash_by_broker: [],
  holdings: [],
  holdings_value: "0",
  holdings_count: 0,
  total_value: "0",
  pending_reconciliation: false,
  cta: "Organize into portfolios",
};

const SECTOR_SUGGESTION: GroupingSuggestion = {
  basis: "SECTOR",
  proposed_name: "Financials",
  keys: [
    { instrument_id: 1, broker_account_id: 7 },
    { instrument_id: 1, broker_account_id: 8 },
  ],
  value: "480000.00",
  rationale: "These 2 holdings are all Financials.",
  suggested_kind: "CAPITAL",
  basket_id: null,
  basket_coverage: null,
  missing_instrument_ids: [],
};

const OVERLAP_SUGGESTION: GroupingSuggestion = {
  basis: "BASKET_OVERLAP",
  proposed_name: "Steady Compounders",
  keys: [
    { instrument_id: 1, broker_account_id: 7 },
    { instrument_id: 2, broker_account_id: 7 },
  ],
  value: "525000.50",
  rationale: "You subscribe to Steady Compounders, and you already hold 11 of its 15 stocks.",
  suggested_kind: "CAPITAL",
  basket_id: 4,
  basket_coverage: "0.7333",
  missing_instrument_ids: [91, 92, 93, 94],
};

function renderSection(overrides: Partial<React.ComponentProps<typeof UnallocatedSection>> = {}) {
  return render(
    <UnallocatedSection
      unallocated={UNALLOCATED}
      rows={ROWS}
      suggestions={[SECTOR_SUGGESTION, OVERLAP_SUGGESTION]}
      suggestionsUnavailableReason={null}
      connectedBrokerCount={2}
      {...overrides}
    />,
  );
}

const TARGETS = [
  { portfolio_id: 7, name: "Swing", kind: "CAPITAL" as const },
  { portfolio_id: 8, name: "Long term", kind: "CAPITAL" as const },
];

/* --------------------------------------------------- adding to an existing portfolio */

describe("adding to a portfolio that already exists is reachable from the page", () => {
  /* It was not, for one deploy. PF9 built the flow and put it behind "+ New portfolio", which is
     where nobody looking to add to an EXISTING portfolio will ever look — Maulik went looking and
     did not find it. These pin the entrance, not just the machinery behind it. */

  it("offers its own button when there is a portfolio to add to", async () => {
    const user = userEvent.setup();
    renderSection({ targets: TARGETS });

    const button = screen.getByRole("button", { name: "Add to a portfolio" });
    await user.click(button);

    expect(screen.getByTestId("target-step")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /Swing/ })).toBeChecked();
  });

  it("hides the button when there is nothing to add to", () => {
    renderSection({ targets: [] });

    expect(screen.queryByRole("button", { name: "Add to a portfolio" })).toBeNull();
  });

  it("Continue is live immediately, because a portfolio is already chosen", async () => {
    const user = userEvent.setup();
    renderSection({ targets: TARGETS });
    await user.click(screen.getByRole("button", { name: "Add to a portfolio" }));

    expect(screen.getByRole("button", { name: "Continue" })).toBeEnabled();
  });
});

/* ------------------------------------------------------------------ §6.6 centrepiece */

describe("§6.6 — the Unallocated section is a centrepiece", () => {
  it("shows unallocated cash and unassigned holdings with one total", () => {
    renderSection();
    const section = screen.getByTestId("unallocated-section");
    expect(section).toHaveAttribute("data-state", "unallocated");
    expect(within(section).getByTestId("unallocated-total")).toHaveTextContent("₹8,30,000.50");
    expect(within(section).getByTestId("unallocated-cash")).toHaveTextContent("₹1,25,000.00");
    expect(within(section).getByTestId("unallocated-holdings")).toHaveTextContent("₹7,05,000.50");
    expect(within(section).getByText(/3 holdings in no portfolio/)).toBeInTheDocument();
    expect(within(section).getByText("Zerodha ₹1,25,000.00")).toBeInTheDocument();
  });

  it("leads with §6.6's own call to action", () => {
    renderSection();
    expect(screen.getByRole("button", { name: "Organize into portfolios" })).toBeInTheDocument();
  });

  it("says nothing is unallocated when nothing is, instead of showing an empty pile", () => {
    renderSection({ unallocated: EMPTY_UNALLOCATED, rows: [] });
    expect(screen.getByTestId("unallocated-section")).toHaveAttribute("data-state", "clear");
    expect(screen.getByText("Nothing unallocated")).toBeInTheDocument();
  });

  it("distinguishes 'we could not ask' from 'nothing is unallocated'", () => {
    renderSection({ unallocated: null });
    const section = screen.getByTestId("unallocated-section");
    expect(section).toHaveAttribute("data-state", "unavailable");
    expect(within(section).getByText(/could not reach the portfolio service/)).toBeInTheDocument();
  });
});

/* ------------------------------------------------------- §6.6 first-run suggestions */

describe("§6.6 — the product actively helps sort the pile", () => {
  it("renders each suggestion with its basis, its rationale and its value", () => {
    renderSection();
    const cards = screen.getAllByTestId("suggestion-card");
    expect(cards).toHaveLength(2);
    /* Ranked by value descending: the model overlap (5.25L) outranks Financials (4.8L). */
    expect(cards[0]).toHaveAttribute("data-basis", "BASKET_OVERLAP");
    expect(within(cards[0] as HTMLElement).getByText("Steady Compounders")).toBeInTheDocument();
    expect(
      within(cards[0] as HTMLElement).getByText(/already hold 11 of its 15 stocks/),
    ).toBeInTheDocument();
    expect(within(cards[0] as HTMLElement).getByTestId("suggestion-coverage")).toHaveTextContent(
      "You hold 73% of this model — 4 stocks in it you do not hold.",
    );
    expect(cards[1]).toHaveAttribute("data-basis", "SECTOR");
    expect(within(cards[1] as HTMLElement).getByText("Same sector")).toBeInTheDocument();
  });

  it("says why there are no suggestions rather than showing an empty list", () => {
    renderSection({ suggestions: [], suggestionsUnavailableReason: "Sectors are not synced yet." });
    expect(screen.getByTestId("suggestions-unavailable")).toHaveTextContent(
      "Sectors are not synced yet.",
    );
  });

  it("accepting a suggestion pre-fills the grouping flow with its name and its holdings", async () => {
    const user = userEvent.setup();
    renderSection();
    const financials = screen
      .getAllByTestId("suggestion-card")
      .find((card) => card.getAttribute("data-basis") === "SECTOR");
    expect(financials).toBeDefined();

    await user.click(
      within(financials as HTMLElement).getByRole("button", { name: "Use this grouping" }),
    );

    const flow = screen.getByTestId("new-portfolio-flow");
    expect(flow).toHaveAttribute("data-step", "holdings");

    /* The name came across… */
    expect(within(flow).getByRole("heading", { name: "Financials" })).toBeInTheDocument();
    /* …and so did both legs of the two-broker holding, priced exactly. */
    expect(within(flow).getByTestId("picker-total")).toHaveTextContent("₹4,80,000.00");
    expect(within(flow).getByTestId("picker-count")).toHaveTextContent("2 holdings · 1 stock");
    expect(within(flow).getByLabelText("Add HDFC Bank to this portfolio")).toBeChecked();
    expect(within(flow).getByLabelText("Add ITC to this portfolio")).not.toBeChecked();
  });
});

/* ------------------------------------------------------------------- §6.7 the flow */

describe("§6.7 — the two-panel picker", () => {
  async function openPicker() {
    const user = userEvent.setup();
    renderSection();
    await user.click(screen.getByRole("button", { name: "Organize into portfolios" }));
    return { user, picker: screen.getByTestId("holdings-picker") };
  }

  it("shows no figure until something is picked, then a live exact total", async () => {
    const { user, picker } = await openPicker();
    expect(within(picker).getByTestId("picker-total")).toHaveTextContent("—");

    await user.click(within(picker).getByRole("button", { name: "Select all unallocated" }));
    expect(within(picker).getByTestId("picker-total")).toHaveTextContent("₹7,05,000.50");
    expect(within(picker).getByTestId("picker-count")).toHaveTextContent("3 holdings · 2 stocks");

    await user.click(within(picker).getByLabelText("Add HDFC Bank to this portfolio"));
    expect(within(picker).getByTestId("picker-total")).toHaveTextContent("₹2,25,000.50");
    expect(within(picker).getByTestId("picker-count")).toHaveTextContent("1 holding · 1 stock");
  });

  it("aggregates a stock held at two brokers and keeps the breakdown on drill-down", async () => {
    const { user, picker } = await openPicker();
    expect(
      within(picker).getByText("HDFC Bank — 320 (Zerodha 200 · Upstox 120)"),
    ).toBeInTheDocument();

    /* The legs allocate separately: one broker's 200 can be picked without the other's 120. */
    await user.click(within(picker).getByLabelText("Add HDFC Bank at Zerodha to this portfolio"));
    expect(within(picker).getByTestId("picker-total")).toHaveTextContent("₹3,00,000.00");
    expect(within(picker).getByTestId("picker-count")).toHaveTextContent("1 holding · 1 stock");
    expect(within(picker).getByTestId("picker-selected")).toHaveTextContent("(1 of 2 brokers)");
  });

  it("offers a quantity per picked leg, capped at its free shares — 0035", async () => {
    /* This test asserted the OPPOSITE until 10 Sep 2026: "offers no partial-quantity control
       anywhere — §4.2, whole holdings only". Maulik asked for the reversal in his own words —
       "one stock can appear in multiple portfolios, so if stock a bought 100 qty for shortterm
       20 for long term 34 for some swing 36 for momentum" — so what is pinned now is where the
       control lives and what it is bounded by, not that it is absent. */
    const { user, picker } = await openPicker();
    await user.click(within(picker).getByLabelText("Add HDFC Bank at Zerodha to this portfolio"));

    const box = within(picker).getByLabelText(
      "Shares of HDFC Bank at Zerodha for this portfolio",
    );

    /* Blank means "all of it", so the untouched flow is still one click and the total is whole. */
    expect(box).toHaveValue("");
    expect(box).toHaveAttribute("placeholder", "200");
    expect(within(picker).getByTestId("picker-total")).toHaveTextContent("₹3,00,000.00");
  });

  it("narrowing a leg narrows the running total in proportion", async () => {
    const { user, picker } = await openPicker();
    await user.click(within(picker).getByLabelText("Add HDFC Bank at Zerodha to this portfolio"));
    const box = within(picker).getByLabelText(
      "Shares of HDFC Bank at Zerodha for this portfolio",
    );

    await user.type(box, "50");

    /* A quarter of the 200-share leg is a quarter of its ₹3,00,000. */
    expect(within(picker).getByTestId("picker-total")).toHaveTextContent("₹75,000.00");
  });

  it("says so when the typed quantity exceeds what is free, before the API has to", async () => {
    const { user, picker } = await openPicker();
    await user.click(within(picker).getByLabelText("Add HDFC Bank at Zerodha to this portfolio"));
    const box = within(picker).getByLabelText(
      "Shares of HDFC Bank at Zerodha for this portfolio",
    );

    await user.type(box, "500");

    expect(box).toHaveAttribute("aria-invalid", "true");
    expect(within(picker).getByText("more than you have")).toBeInTheDocument();
  });

  it("forgets a typed quantity when its leg is un-ticked", async () => {
    /* Otherwise a leg re-ticked later silently carries a number set for a different portfolio. */
    const { user, picker } = await openPicker();
    const tick = within(picker).getByLabelText("Add HDFC Bank at Zerodha to this portfolio");
    await user.click(tick);
    await user.type(
      within(picker).getByLabelText("Shares of HDFC Bank at Zerodha for this portfolio"),
      "50",
    );
    await user.click(tick);
    await user.click(tick);

    expect(
      within(picker).getByLabelText("Shares of HDFC Bank at Zerodha for this portfolio"),
    ).toHaveValue("");
  });

  it("carries the selection through kind, name and benchmark to a confirm", async () => {
    const user = userEvent.setup();
    const onCreate = vi.fn();
    renderSection({ onCreate });
    await user.click(screen.getByRole("button", { name: "Organize into portfolios" }));
    await user.click(screen.getByRole("button", { name: "Select all unallocated" }));
    await user.click(screen.getByRole("button", { name: "Continue" }));

    /* §4.1's explanation is on screen at the moment of choice, not behind a link. */
    const kind = screen.getByTestId("kind-choice");
    expect(
      within(kind).getByText("Capital portfolios plus Unallocated add up to your net worth, to the paisa."),
    ).toBeInTheDocument();
    expect(
      within(kind).getByText(
        "Monitoring view — overlaps with other portfolios, excluded from totals.",
      ),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Continue" }));
    await user.type(screen.getByLabelText("Name"), "Core");
    await user.click(screen.getByRole("button", { name: "Continue" }));

    expect(screen.getByTestId("review-step")).toHaveTextContent("Nifty 500");
    await user.click(screen.getByRole("button", { name: "Create portfolio" }));

    expect(onCreate).toHaveBeenCalledTimes(1);
    const draft = onCreate.mock.calls[0]?.[0] as {
      name: string;
      kind: string;
      keys: Array<{ instrument_id: number; broker_account_id: number }>;
      benchmark: string;
    };
    expect(draft.name).toBe("Core");
    expect(draft.kind).toBe("CAPITAL");
    expect(draft.benchmark).toBe("Nifty 500");
    expect(draft.keys).toHaveLength(3);
  });

  it("offers all five §6.7 starting points from + New portfolio", async () => {
    const user = userEvent.setup();
    renderSection();
    await user.click(screen.getByRole("button", { name: "+ New portfolio" }));
    const options = screen.getByTestId("start-options");
    for (const label of [
      "From a subscribed basket",
      "From my screen",
      "From my strategy",
      "From broker holdings",
      "Empty",
    ]) {
      expect(within(options).getByText(label)).toBeInTheDocument();
    }
  });

  it("does not send a user with no subscribed models to the catalog", async () => {
    const user = userEvent.setup();
    const { container } = renderSection();
    await user.click(screen.getByRole("button", { name: "+ New portfolio" }));
    await user.click(screen.getByText("From a subscribed basket"));

    const step = screen.getByTestId("source-step");
    expect(within(step).getByText(/do not subscribe to any published model yet/)).toBeInTheDocument();
    expect(
      within(step).getByRole("button", { name: "Group holdings I already own" }),
    ).toBeInTheDocument();
    expect(container.querySelector('a[href*="/explore"]')).toBeNull();
    expect(container.querySelector('a[href*="/baskets"]')).toBeNull();
  });
});

/* --------------------------------------------------------- acceptance criterion 8 */

describe("acceptance criterion 8 — nothing connected leads to Connect your broker", () => {
  it("offers the broker, never the basket catalog", () => {
    const { container } = renderSection({
      unallocated: EMPTY_UNALLOCATED,
      rows: [],
      connectedBrokerCount: 0,
      suggestions: [],
    });

    const section = screen.getByTestId("unallocated-section");
    expect(section).toHaveAttribute("data-state", "no-broker");

    const connect = within(section).getByRole("link", { name: "Connect your broker" });
    expect(connect).toHaveAttribute("href", "/brokers");

    expect(container.querySelector('a[href*="/explore"]')).toBeNull();
    expect(container.querySelector('a[href*="/baskets"]')).toBeNull();
    expect(container.querySelector('a[href*="/discover"]')).toBeNull();
    expect(screen.queryByText(/explore baskets/i)).toBeNull();
    expect(screen.queryByText(/browse the catalog/i)).toBeNull();
  });

  it("holds even when the API did not answer at all", () => {
    renderSection({ unallocated: null, rows: [], connectedBrokerCount: 0 });
    const section = screen.getByTestId("unallocated-section");
    expect(section).toHaveAttribute("data-state", "no-broker");
    expect(within(section).getByRole("link", { name: "Connect your broker" })).toHaveAttribute(
      "href",
      "/brokers",
    );
  });
});
