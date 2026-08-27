import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const push = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push }),
}));

import { GoalComposer } from "@/components/discover/goal-composer";
import { DEFAULT_PREFERENCES } from "@/lib/discover/match";

beforeEach(() => {
  push.mockClear();
});

describe("the composer reads as a sentence", () => {
  it("asks the question in the reader's terms, not the product's", () => {
    render(<GoalComposer />);
    expect(screen.getByText("Find a basket for the way you invest")).toBeInTheDocument();
  });

  it("offers the five editable fields the brief asks for", () => {
    render(<GoalComposer />);
    for (const label of [
      "Investment goal",
      "Investment horizon",
      "Risk preference",
      "Rebalance preference",
    ]) {
      expect(screen.getByLabelText(label)).toBeInTheDocument();
    }
    expect(screen.getByLabelText("Investment amount in rupees")).toBeInTheDocument();
  });

  it("labels every control for a screen reader, since the labels are visually a sentence", () => {
    render(<GoalComposer />);
    for (const control of screen.getAllByRole("combobox")) {
      expect(control).toHaveAccessibleName();
    }
    expect(screen.getByRole("textbox")).toHaveAccessibleName();
  });

  it("starts from the preferences it was given, so a shared link reopens as it was sent", () => {
    render(
      <GoalComposer
        initial={{ ...DEFAULT_PREFERENCES, risk: "lower", amount: 250_000, rebalance: "MONTHLY" }}
      />,
    );
    expect(screen.getByLabelText("Risk preference")).toHaveValue("lower");
    expect(screen.getByLabelText("Investment amount in rupees")).toHaveValue("250000");
    expect(screen.getByLabelText("Rebalance preference")).toHaveValue("MONTHLY");
  });
});

describe("it filters, and says that it filters", () => {
  it("calls the action a filter rather than a recommendation", () => {
    render(<GoalComposer />);
    expect(screen.getByTestId("show-matching").textContent).toBe("Show matching baskets");
  });

  it("says plainly that this is not advice, next to the control that runs it", () => {
    render(<GoalComposer />);
    const form = screen.getByTestId("goal-composer");
    expect(form.textContent).toMatch(/not a registered adviser/i);
    expect(form.textContent).toMatch(/nothing here is a recommendation/i);
  });

  it("never uses advice language anywhere in the form", () => {
    render(<GoalComposer />);
    const text = screen.getByTestId("goal-composer").textContent ?? "";
    expect(text).not.toMatch(/best for you|recommended for you|we recommend|suitable for you/i);
  });
});

describe("submitting", () => {
  it("puts the preferences in the URL, so the result is a link", async () => {
    const user = userEvent.setup();
    render(<GoalComposer />);
    await user.selectOptions(screen.getByLabelText("Risk preference"), "lower");
    await user.click(screen.getByTestId("show-matching"));
    expect(push).toHaveBeenCalledTimes(1);
    const target = push.mock.calls[0]![0] as string;
    expect(target).toMatch(/^\/discover\?/);
    expect(new URLSearchParams(target.split("?")[1]).get("risk")).toBe("lower");
  });

  it("accepts an amount typed the way a person types it", async () => {
    const user = userEvent.setup();
    render(<GoalComposer />);
    const amount = screen.getByLabelText("Investment amount in rupees");
    await user.clear(amount);
    await user.type(amount, "2,50,000");
    await user.click(screen.getByTestId("show-matching"));
    const target = push.mock.calls[0]![0] as string;
    expect(new URLSearchParams(target.split("?")[1]).get("amount")).toBe("250000");
  });

  it("refuses an impossible amount instead of filtering on it", async () => {
    // A negative amount used to survive by having its sign stripped.
    const user = userEvent.setup();
    render(<GoalComposer />);
    const amount = screen.getByLabelText("Investment amount in rupees");
    await user.clear(amount);
    await user.type(amount, "-5000");
    await user.click(screen.getByTestId("show-matching"));
    const target = push.mock.calls[0]![0] as string;
    expect(new URLSearchParams(target.split("?")[1]).get("amount")).toBe(
      String(DEFAULT_PREFERENCES.amount),
    );
  });

  it("shows the reader the amount it actually used", async () => {
    const user = userEvent.setup();
    render(<GoalComposer />);
    const amount = screen.getByLabelText("Investment amount in rupees");
    await user.clear(amount);
    await user.type(amount, "abc");
    await user.click(screen.getByTestId("show-matching"));
    expect(amount).toHaveValue(String(DEFAULT_PREFERENCES.amount));
  });
});
