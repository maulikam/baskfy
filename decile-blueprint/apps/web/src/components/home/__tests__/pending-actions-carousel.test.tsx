import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  PendingActionsCarousel,
  TERMINATOR_COPY,
} from "@/components/home/pending-actions-carousel";
import type { PendingActionBrief } from "@/lib/investments/fetch";

afterEach(cleanup);

const ACTIONS: PendingActionBrief[] = [
  {
    id: "1",
    type: "DRIFT",
    title: "Incorrect holdings — Fix now",
    body: "Stocks were sold directly at the broker.",
  },
  { id: "2", type: "REBALANCE_AVAILABLE", title: "Rebalance update available", body: null },
];

function ok() {
  return vi.fn().mockResolvedValue({ ok: true, message: "Dismissed." });
}

describe("the pending-actions carousel", () => {
  it("renders one card per open action", () => {
    render(<PendingActionsCarousel actions={ACTIONS} dismiss={ok()} />);
    expect(screen.getByText("Incorrect holdings — Fix now")).toBeTruthy();
    expect(screen.getByText("Rebalance update available")).toBeTruthy();
    expect(screen.getByText(/2 waiting on you/)).toBeTruthy();
  });

  it("always ends in the terminator card, so a stopped carousel is not ambiguous", () => {
    render(<PendingActionsCarousel actions={ACTIONS} dismiss={ok()} />);
    expect(screen.getByTestId("pending-actions-terminator").textContent).toBe(TERMINATOR_COPY);
  });

  it("renders the terminator alone when nothing needs a decision", () => {
    render(<PendingActionsCarousel actions={[]} dismiss={ok()} />);
    expect(screen.getByTestId("pending-actions-terminator")).toBeTruthy();
    expect(screen.queryByText(/waiting on you/)).toBeNull();
  });

  it("dismisses through the injected server action and removes the card at once", async () => {
    const user = userEvent.setup();
    const dismiss = ok();
    render(<PendingActionsCarousel actions={ACTIONS} dismiss={dismiss} />);

    await user.click(screen.getByTestId("dismiss-1"));

    expect(dismiss).toHaveBeenCalledWith("1");
    await waitFor(() => expect(screen.queryByText("Incorrect holdings — Fix now")).toBeNull());
    expect(screen.getByText("Rebalance update available")).toBeTruthy();
  });

  it("puts the card back with the reason when the dismiss fails", async () => {
    const user = userEvent.setup();
    const dismiss = vi
      .fn()
      .mockResolvedValue({ ok: false, message: "We could not reach the service." });
    render(<PendingActionsCarousel actions={ACTIONS} dismiss={dismiss} />);

    await user.click(screen.getByTestId("dismiss-1"));

    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toMatch(/could not reach/i),
    );
    // The whole point: a card that vanished on a failed request would be the product deciding
    // for the reader that they had dealt with it.
    expect(screen.getByText("Incorrect holdings — Fix now")).toBeTruthy();
  });

  it("offers no way to act on an action from the card — dismiss is not resolve", () => {
    render(<PendingActionsCarousel actions={ACTIONS} dismiss={ok()} />);
    const labels = screen
      .getAllByRole("button")
      .map((node) => node.getAttribute("aria-label") ?? "");
    expect(labels.every((label) => label.startsWith("Dismiss:"))).toBe(true);
  });
});
