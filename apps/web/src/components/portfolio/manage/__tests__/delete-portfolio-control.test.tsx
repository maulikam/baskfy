import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { DeletePortfolioControl } from "@/components/portfolio/manage/delete-portfolio-control";
import { ApiError } from "@/lib/api/errors";
import { canConfirmDelete, deleteDialogCopy } from "@/lib/portfolio/delete-portfolio";

const state = vi.hoisted(() => ({
  calls: [] as unknown[],
  fail: null as Error | null,
  push: vi.fn(),
  refresh: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: state.push, refresh: state.refresh }),
}));
vi.mock("@/lib/portfolios/queries", async () => {
  const { useState } = await import("react");
  return {
    useDeletePortfolio: () => {
      const [error, setError] = useState<Error | null>(null);
      return {
        isPending: false,
        error,
        reset: () => setError(null),
        mutateAsync: (input: unknown) => {
          state.calls.push(input);
          if (state.fail) {
            setError(state.fail);
            return Promise.reject(state.fail);
          }
          return Promise.resolve();
        },
      };
    },
  };
});

const empty = { id: 4, name: "Swing", holdingsCount: 0 };
const held = { id: 3, name: "Zerodha holdings", holdingsCount: 3 };

beforeEach(() => {
  state.calls = [];
  state.fail = null;
  state.push.mockReset();
  state.refresh.mockReset();
});

describe("delete-portfolio copy", () => {
  it("asks for the typed name only when holdings are recorded", () => {
    expect(deleteDialogCopy(empty).needsTypedName).toBe(false);
    expect(deleteDialogCopy(held).needsTypedName).toBe(true);
    expect(canConfirmDelete(empty, "")).toBe(true);
    expect(canConfirmDelete(held, "Zerodha")).toBe(false);
    expect(canConfirmDelete(held, " Zerodha holdings ")).toBe(true);
  });

  it("names the holdings, the broker and that it cannot be undone", () => {
    const copy = deleteDialogCopy(held);
    expect(copy.title).toContain("Zerodha holdings");
    expect(copy.holdingsLine).toContain("3 holdings");
    expect(copy.brokerLine).toContain("No order is placed");
    expect(copy.undoLine).toContain("cannot be undone");
  });
});

describe("DeletePortfolioControl", () => {
  it("deletes an empty portfolio after one confirm and goes to the overview", async () => {
    render(<DeletePortfolioControl target={empty} />);
    fireEvent.click(screen.getByTestId("delete-portfolio-open"));
    expect(screen.getByTestId("delete-portfolio-dialog")).toHaveTextContent("Swing");
    expect(screen.queryByTestId("delete-portfolio-typed")).toBeNull();
    fireEvent.click(screen.getByTestId("delete-portfolio-confirm"));
    await waitFor(() => expect(state.push).toHaveBeenCalledWith("/portfolio/overview"));
    expect(state.calls).toEqual([{ id: 4, confirmHoldings: false }]);
  });

  it("holds the button until the name is typed when holdings are recorded", async () => {
    render(<DeletePortfolioControl target={held} />);
    fireEvent.click(screen.getByTestId("delete-portfolio-open"));
    expect(screen.getByTestId("delete-portfolio-holdings")).toHaveTextContent("3 holdings");
    expect(screen.getByTestId("delete-portfolio-undo")).toHaveTextContent("cannot be undone");
    const confirm = screen.getByTestId("delete-portfolio-confirm");
    expect(confirm).toBeDisabled();
    fireEvent.change(screen.getByTestId("delete-portfolio-typed"), {
      target: { value: "Zerodha holdings" },
    });
    expect(confirm).toBeEnabled();
    fireEvent.click(confirm);
    await waitFor(() => expect(state.push).toHaveBeenCalledWith("/portfolio/overview"));
    expect(state.calls).toEqual([{ id: 3, confirmHoldings: true }]);
  });

  it("asks for the typed name and resends when the API counts holdings the page did not list", async () => {
    state.fail = new ApiError("Swing still records 2 holdings. Pass confirm=holdings to delete it anyway.", null, 409);
    render(<DeletePortfolioControl target={empty} />);
    fireEvent.click(screen.getByTestId("delete-portfolio-open"));
    fireEvent.click(screen.getByTestId("delete-portfolio-confirm"));
    const error = await screen.findByTestId("delete-portfolio-error");
    expect(error).not.toHaveTextContent("confirm=holdings");
    const confirm = screen.getByTestId("delete-portfolio-confirm");
    expect(confirm).toBeDisabled();
    state.fail = null;
    fireEvent.change(screen.getByTestId("delete-portfolio-typed"), { target: { value: "Swing" } });
    fireEvent.click(confirm);
    await waitFor(() => expect(state.push).toHaveBeenCalledWith("/portfolio/overview"));
    expect(state.calls).toEqual([
      { id: 4, confirmHoldings: false },
      { id: 4, confirmHoldings: true },
    ]);
  });

  it("shows the API's refusal and stays on the page", async () => {
    state.fail = new Error("Swing is linked to 1 basket investment.");
    render(<DeletePortfolioControl target={empty} />);
    fireEvent.click(screen.getByTestId("delete-portfolio-open"));
    fireEvent.click(screen.getByTestId("delete-portfolio-confirm"));
    expect(await screen.findByTestId("delete-portfolio-error")).toHaveTextContent(
      "linked to 1 basket investment",
    );
    expect(state.push).not.toHaveBeenCalled();
  });
});
