/**
 * §6.7's confirm button, once it is wired to a write that can refuse.
 *
 * The flow already carried a selection through to a draft; what these assert is the half that
 * only matters when the write is real — that a refusal reaches the person who clicked, that a
 * success closes the loop, and that neither can happen twice from one click.
 *
 * Criterion 2's conflict is the case worth protecting. The API answers "HDFC Bank is already in
 * Long term", which names both the stock and where it went. A UI that swallowed that and said
 * "Something went wrong" would leave the user with no way to act on it.
 */

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { NewPortfolioFlow, type CreateOutcome } from "@/components/portfolio/new-portfolio-flow";
import type { AggregatedHolding, HoldingBrokerLine } from "@/lib/portfolio/organize";

/* The same fixture shape `unallocated-organize.test.tsx` builds — a stock at two brokers, which
   is §6.7's aggregated-display case and the one whose keys the draft must carry. */
function leg(brokerAccountId: number, label: string, quantity: string): HoldingBrokerLine {
  return {
    broker: { broker_account_id: brokerAccountId, broker_id: label.toLowerCase(), label },
    quantity,
    value: null,
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

const ROWS: readonly AggregatedHolding[] = [
  {
    instrument: { instrument_id: 11, symbol: "HDFCBANK", name: "HDFC Bank" },
    quantity: "320",
    price: null,
    price_as_of: null,
    value: "480000.00",
    allocation: null,
    allocated: false,
    split_across_portfolios: false,
    monitoring_views: [],
    brokers: [leg(3, "Zerodha", "200"), leg(4, "Upstox", "120")],
    pending_reconciliation: false,
  },
];

/** Walk the flow to the review step, so each test starts where confirm lives. */
async function reachReview(onCreate: (draft: never) => Promise<CreateOutcome>) {
  const user = userEvent.setup();
  render(
    <NewPortfolioFlow
      rows={ROWS}
      onCancel={vi.fn()}
      onCreate={onCreate as never}
      seed={{ start: "HOLDINGS", step: "holdings" }}
    />,
  );
  await user.click(screen.getByRole("button", { name: "Select all unallocated" }));
  await user.click(screen.getByRole("button", { name: "Continue" }));
  await user.click(screen.getByRole("button", { name: "Continue" }));
  await user.type(screen.getByLabelText("Name"), "Core");
  await user.click(screen.getByRole("button", { name: "Continue" }));
  return user;
}

describe("a refusal reaches the person who clicked", () => {
  it("shows the server's own sentence, naming the stock and the portfolio", async () => {
    const onCreate = vi.fn(
      (): Promise<CreateOutcome> =>
        Promise.resolve({ ok: false, reason: "HDFC Bank is already in Long term." }),
    );
    const user = await reachReview(onCreate);

    await user.click(screen.getByRole("button", { name: "Create portfolio" }));

    const alert = await screen.findByTestId("create-refusal");
    // The two facts that let the user fix it survive the trip to the screen.
    expect(alert).toHaveTextContent("HDFC Bank");
    expect(alert).toHaveTextContent("Long term");
    expect(alert).toHaveAttribute("role", "alert");
  });

  it("keeps the selection on screen so the user can change the kind and retry", async () => {
    const onCreate = vi.fn(
      (): Promise<CreateOutcome> => Promise.resolve({ ok: false, reason: "Already allocated." }),
    );
    const user = await reachReview(onCreate);
    await user.click(screen.getByRole("button", { name: "Create portfolio" }));

    await screen.findByTestId("create-refusal");
    // Still on review, with the name intact — a refusal that reset the flow would make the user
    // rebuild a forty-holding selection to change one radio button.
    expect(screen.getByTestId("review-step")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create portfolio" })).toBeEnabled();
  });

  it("does not show a refusal before anything has been refused", async () => {
    const onCreate = vi.fn(
      (): Promise<CreateOutcome> => Promise.resolve({ ok: true, portfolioId: 7 }),
    );
    await reachReview(onCreate);
    expect(screen.queryByTestId("create-refusal")).toBeNull();
  });
});

describe("a success closes the loop", () => {
  it("reports the new portfolio's id to the host", async () => {
    const onCreated = vi.fn();
    const user = userEvent.setup();
    render(
      <NewPortfolioFlow
        rows={ROWS}
        onCancel={vi.fn()}
        onCreate={() => Promise.resolve({ ok: true, portfolioId: 42 })}
        onCreated={onCreated}
        seed={{ start: "HOLDINGS", step: "holdings", name: "Core" }}
      />,
    );
    await user.click(screen.getByRole("button", { name: "Select all unallocated" }));
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await user.click(screen.getByRole("button", { name: "Create portfolio" }));

    await waitFor(() => expect(onCreated).toHaveBeenCalledWith(42));
    expect(screen.queryByTestId("create-refusal")).toBeNull();
  });

  it("a handler that reports nothing is not treated as a refusal", async () => {
    // A spy in a test that only cares the click fired resolves to undefined. Showing an error for
    // that would put a failure on screen for a create that may well have worked.
    const onCreate = vi.fn(() => Promise.resolve(undefined));
    const user = await reachReview(onCreate as never);
    await user.click(screen.getByRole("button", { name: "Create portfolio" }));

    await waitFor(() => expect(onCreate).toHaveBeenCalledTimes(1));
    expect(screen.queryByTestId("create-refusal")).toBeNull();
  });
});

describe("one click is one write", () => {
  it("disables confirm while the write is in flight", async () => {
    let release: (value: CreateOutcome) => void = () => undefined;
    const onCreate = vi.fn(
      () =>
        new Promise<CreateOutcome>((resolve) => {
          release = resolve;
        }),
    );
    const user = await reachReview(onCreate);

    await user.click(screen.getByRole("button", { name: "Create portfolio" }));

    // The button says what is happening and refuses a second click — a double submit here would
    // ask the API to allocate the same holdings twice, and criterion 2 would refuse the second
    // with a conflict the user did not cause.
    const button = await screen.findByRole("button", { name: "Creating…" });
    expect(button).toBeDisabled();
    await user.click(button);
    expect(onCreate).toHaveBeenCalledTimes(1);

    release({ ok: true, portfolioId: 1 });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Create portfolio" })).toBeEnabled(),
    );
  });
});

describe("the stale note is gone", () => {
  it("does not tell the user the flow cannot save when it can", async () => {
    await reachReview(() => Promise.resolve({ ok: true, portfolioId: 1 }));
    const review = screen.getByTestId("review-step");
    expect(within(review).queryByText(/does not save/i)).toBeNull();
  });
});
