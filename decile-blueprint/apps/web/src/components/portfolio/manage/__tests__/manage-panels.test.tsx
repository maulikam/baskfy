import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  ManagePortfoliosDrawer,
  type ManageHandlers,
} from "@/components/portfolio/manage/manage-drawer";
import { BROKERS, CAPITAL, ROWS, VIEWS } from "@/components/portfolio/manage/__tests__/fixtures";
import type { ManageActionId } from "@/lib/portfolio/manage";

/**
 * The four panels whose copy carries a rule rather than a label.
 *
 * Move, because a preview that shows one side of a transfer has hidden the half the user did not
 * initiate. Views, because "this reallocates nothing" is not inferable from a form of ticked
 * boxes. Delete, because the fear it has to answer first is *"will this sell my shares"*. And
 * failure, because Baskfy has shipped a silently swallowed write twice in one week and this
 * drawer is where that stops.
 */

function openAt(panel: ManageActionId, handlers: ManageHandlers = {}) {
  const user = userEvent.setup();
  render(
    <ManagePortfoliosDrawer
      open
      onOpenChange={() => undefined}
      capital={CAPITAL}
      views={VIEWS}
      rows={ROWS}
      brokers={BROKERS}
      openReconciliationCount={0}
      initialAction={panel}
      handlers={handlers}
    />,
  );
  return user;
}

/** The shipped picker's checkbox for a whole holding. */
function tick(name: string) {
  return screen.getByLabelText(`Add ${name} to this portfolio`);
}

describe("moving holdings between capital portfolios", () => {
  it("move: shows what the source loses and what the destination gains, before anything is written", async () => {
    const transfer = vi.fn();
    const user = openAt("move", { transfer });
    await user.click(tick("ITC Ltd"));

    const source = screen.getByTestId("preview-source");
    const destination = screen.getByTestId("preview-destination");

    expect(source).toHaveTextContent("Leaves");
    expect(source).toHaveTextContent("Long term");
    expect(source).toHaveTextContent("10,000");
    expect(destination).toHaveTextContent("Arrives in");
    expect(destination).toHaveTextContent("Momentum");
    expect(destination).toHaveTextContent("10,000");

    /* The preview is a preview: nothing has been sent by looking at it. */
    expect(transfer).not.toHaveBeenCalled();
  });

  it("move: says what a transfer does to net worth, which is nothing", async () => {
    const user = openAt("move");
    await user.click(tick("ITC Ltd"));
    expect(screen.getByTestId("net-worth-notice")).toHaveTextContent("Net worth does not change");
    expect(screen.getByTestId("exclusivity-notice")).toHaveTextContent(
      "exactly one capital portfolio",
    );
  });

  it("move: refuses more shares than the source holds, and the button says why it is off", async () => {
    const transfer = vi.fn();
    const user = openAt("move", { transfer });
    await user.click(tick("HDFC Bank"));

    const box = screen.getByTestId("picker-quantity-102:11");
    await user.clear(box);
    await user.type(box, "45");

    /* The picker's ceiling is the route's, which is larger than a move's. Said out loud, so the
       two numbers do not read as a bug. */
    expect(screen.getByTestId("move-ceiling-notice")).toHaveTextContent(
      "A move takes only what Long term itself holds",
    );

    const blocks = screen.getByTestId("preview-blocks");
    expect(blocks).toHaveTextContent("Long term holds 30 of HDFCBANK at Zerodha");
    expect(blocks).toHaveTextContent("Cannot be moved.");
    expect(screen.getByTestId("transfer-blocked-reason")).toHaveTextContent("cannot be moved");
    expect(screen.getByRole("button", { name: /move into momentum/i })).toBeDisabled();
  });

  it("move: a passing preview sends exactly the shares it previewed", async () => {
    const transfer = vi.fn().mockResolvedValue({ ok: true, portfolioId: 2 });
    const user = openAt("move", { transfer });
    await user.click(tick("ITC Ltd"));
    await user.click(screen.getByRole("button", { name: /move into momentum/i }));

    expect(transfer).toHaveBeenCalledWith({
      intent: "MOVE",
      destinationPortfolioId: 2,
      sourcePortfolioId: 1,
      holdings: [{ instrument_id: 101, broker_account_id: 11, quantity: "100" }],
    });
    expect(screen.getByTestId("manage-confirmation")).toHaveTextContent("Moved into Momentum");
  });

  it("interaction: undo — a completed move can be undone, and the undo is a move BACK", async () => {
    /* The brief asks for undo on organisational changes. A move is the one write here that is
       exactly reversible, and its reverse is the same request with the two portfolios swapped —
       there is no rollback in this product, only a second move. */
    const transfer = vi.fn().mockResolvedValue({ ok: true, portfolioId: 2 });
    const user = openAt("move", { transfer });
    await user.click(tick("ITC Ltd"));
    await user.click(screen.getByRole("button", { name: /move into momentum/i }));

    await user.click(screen.getByTestId("manage-undo"));

    expect(transfer).toHaveBeenLastCalledWith({
      intent: "MOVE",
      destinationPortfolioId: 1,
      sourcePortfolioId: 2,
      holdings: [{ instrument_id: 101, broker_account_id: 11, quantity: "100" }],
    });
    expect(screen.getByTestId("manage-confirmation")).toHaveTextContent("Moved back into");
  });

  it("fail: a failed move offers no undo, because nothing moved", async () => {
    const transfer = vi.fn().mockResolvedValue({ ok: false, reason: "The broker said no." });
    const user = openAt("move", { transfer });
    await user.click(tick("ITC Ltd"));
    await user.click(screen.getByRole("button", { name: /move into momentum/i }));

    expect(screen.queryByTestId("manage-undo")).not.toBeInTheDocument();
  });

  it("interaction: an undo that fails says the shares are where the move left them", async () => {
    /* Silence here would read as "put back", which is the one thing it is not. */
    const transfer = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, portfolioId: 2 })
      .mockResolvedValueOnce({ ok: false, reason: "The allocation is locked for reconciliation." });
    const user = openAt("move", { transfer });
    await user.click(tick("ITC Ltd"));
    await user.click(screen.getByRole("button", { name: /move into momentum/i }));
    await user.click(screen.getByTestId("manage-undo"));

    const failure = await screen.findByTestId("manage-undo-failed");
    expect(failure).toHaveTextContent("The allocation is locked for reconciliation.");
    expect(failure).toHaveTextContent("where the move left them");
  });

  it("move: an unpriced holding says so where its value would be", async () => {
    const user = openAt("assign");
    await user.click(tick("Tata Consultancy"));
    /* Never a bare dash, and never a plausible zero: the reason stands where the figure would. */
    expect(screen.getByTestId("preview-lines")).toHaveTextContent("No price today");
  });
});

describe("monitoring views", () => {
  it("view: states in words that making one reallocates nothing", () => {
    openAt("watch");
    const notice = screen.getByTestId("view-ownership-notice");
    expect(notice).toHaveTextContent("changes no ownership");
    expect(notice).toHaveTextContent("stays in whichever capital portfolio");
    /* The brief's own required sentence, verbatim. */
    expect(notice).toHaveTextContent(
      "Monitoring views may contain overlapping holdings and are excluded from total portfolio value.",
    );
  });

  it("view: offers no share-count box, because a view has nowhere to put one", () => {
    openAt("watch");
    const picker = screen.getByTestId("view-name-picker");
    /* A quantity sent for a MONITORING view is accepted and ignored by the API. A box for it
       would be a control that changes nothing — exactly what this leaf's gate forbids. */
    expect(within(picker).queryAllByLabelText(/^Shares of/)).toHaveLength(0);
    expect(within(picker).queryAllByRole("spinbutton")).toHaveLength(0);
  });

  it("view: every name in the picker says where it stays", () => {
    openAt("watch");
    const picker = screen.getByTestId("view-name-picker");
    expect(picker).toHaveTextContent("already 100 in Long term — and it stays there.");
    expect(picker).toHaveTextContent("unallocated — and it stays there.");
  });

  it("view: creating one sends a MONITORING portfolio and no share counts", async () => {
    const create = vi.fn().mockResolvedValue({ ok: true, portfolioId: 9 });
    const user = openAt("watch", { create });

    await user.selectOptions(screen.getByLabelText("View"), "NEW");
    await user.type(screen.getByLabelText("Name"), "Dividend payers");
    await user.click(screen.getByLabelText("Watch ITC Ltd in this view"));
    await user.click(screen.getByRole("button", { name: /create this view/i }));

    expect(create).toHaveBeenCalledTimes(1);
    const draft = create.mock.calls[0]?.[0] as { kind: string; benchmark: string; keys: unknown[] };
    expect(draft.kind).toBe("MONITORING");
    expect(draft.keys).toEqual([{ instrument_id: 101, broker_account_id: 11 }]);
    expect(screen.getByTestId("manage-confirmation")).toHaveTextContent(
      "Dividend payers was created as a monitoring view",
    );
  });

  it("view: adding to an existing one sends names with a null quantity", async () => {
    const transfer = vi.fn().mockResolvedValue({ ok: true, portfolioId: 3 });
    const user = openAt("watch", { transfer });

    await user.click(screen.getByLabelText("Watch HDFC Bank in this view"));
    await user.click(screen.getByRole("button", { name: /add to this view/i }));

    expect(transfer).toHaveBeenCalledWith({
      intent: "WATCH",
      destinationPortfolioId: 3,
      sourcePortfolioId: null,
      holdings: [{ instrument_id: 102, broker_account_id: 11, quantity: null }],
    });
  });

  it("view: a lens never shows a side that loses anything", async () => {
    const user = openAt("watch");
    await user.click(screen.getByLabelText("Watch ITC Ltd in this view"));
    expect(screen.queryByTestId("preview-source")).not.toBeInTheDocument();
    expect(screen.getByTestId("preview-destination")).toHaveTextContent("Watched by");
  });
});

describe("deleting a portfolio", () => {
  it("delete: names what is lost, and says the holdings return to unallocated", () => {
    openAt("delete");
    const impact = screen.getByTestId("delete-impact");
    expect(impact).toHaveTextContent("What deleting Long term does");
    expect(impact).toHaveTextContent("4,80,000");

    const fate = screen.getByTestId("delete-holdings-fate");
    expect(fate).toHaveTextContent("NOT deleted");
    expect(fate).toHaveTextContent("stay in your demat account");
    expect(fate).toHaveTextContent("return to Unallocated");

    const losses = screen.getByTestId("delete-losses");
    expect(losses).toHaveTextContent("Its recorded value history");
    expect(losses).toHaveTextContent("Its cash assignments and dividend records");
    expect(screen.getByTestId("delete-children")).toHaveTextContent("promoted to the top level");
  });

  it("delete: stays off until the portfolio's own name is typed", async () => {
    const remove = vi.fn();
    const user = openAt("delete", { remove });

    const button = screen.getByRole("button", { name: /delete long term/i });
    expect(button).toBeDisabled();
    expect(screen.getByTestId("delete-blocked-reason")).toBeInTheDocument();

    await user.type(screen.getByLabelText(/type .* to confirm/i), "Long ter");
    expect(button).toBeDisabled();

    await user.type(screen.getByLabelText(/type .* to confirm/i), "m");
    expect(button).toBeEnabled();

    await user.click(button);
    expect(remove).toHaveBeenCalledWith(1);
  });

  it("delete: a monitoring view says nothing is unfiled, because it owns nothing", async () => {
    const user = openAt("delete");
    await user.selectOptions(screen.getByLabelText("Portfolio to delete"), "3");
    expect(screen.getByTestId("delete-holdings-fate")).toHaveTextContent(
      "owns none of the 5 holdings it watches",
    );
  });

  it("delete: a figure it cannot show carries its reason rather than a dash", () => {
    render(
      <ManagePortfoliosDrawer
        open
        onOpenChange={() => undefined}
        capital={[{ ...CAPITAL[0]!, value: "", cash: "" }]}
        views={[]}
        rows={ROWS}
        initialAction="delete"
      />,
    );
    const impact = screen.getByTestId("delete-impact");
    expect(impact).toHaveTextContent("Needs more data");
    expect(impact.textContent ?? "").not.toContain("Nothing in this portfolio has been priced yet.");
    expect(impact.querySelector('[title="Nothing in this portfolio has been priced yet."]')).not.toBeNull();
    /* The rule is about a FIGURE that is a bare dash, not about punctuation: no element in the
       panel may be nothing but an em dash where a number was expected. */
    expect(within(impact).queryAllByText("—")).toHaveLength(0);
  });
});

describe("a write that does not succeed", () => {
  it("fails: a refusal shows the service's own sentence and leaves the form open with what was typed", async () => {
    const rename = vi
      .fn()
      .mockResolvedValue({ ok: false, reason: "A portfolio called Swing already exists." });
    const user = openAt("rename", { rename });

    const box = screen.getByLabelText("New name");
    await user.clear(box);
    await user.type(box, "Swing");
    await user.click(screen.getByRole("button", { name: "Rename" }));

    const failure = screen.getByTestId("write-failure");
    expect(failure).toHaveAttribute("role", "alert");
    expect(failure).toHaveTextContent("A portfolio called Swing already exists.");
    expect(failure).toHaveTextContent("Not saved.");

    /* The three things a failed save must not do: close the drawer, leave the panel, or lose
       what the person entered. */
    expect(screen.getByTestId("manage-drawer")).toBeInTheDocument();
    expect(screen.getByTestId("rename-panel")).toBeInTheDocument();
    expect(box).toHaveValue("Swing");
    expect(screen.queryByTestId("manage-confirmation")).not.toBeInTheDocument();
  });

  it("fails: a refusal with no reason at all still produces a sentence", async () => {
    const rename = vi.fn().mockResolvedValue({ ok: false, reason: "   " });
    const user = openAt("rename", { rename });

    await user.clear(screen.getByLabelText("New name"));
    await user.type(screen.getByLabelText("New name"), "Swing");
    await user.click(screen.getByRole("button", { name: "Rename" }));

    const failure = screen.getByTestId("write-failure");
    expect(failure).toHaveTextContent("did not save");
    expect(failure).toHaveTextContent("Nothing was changed");
  });

  it("fails: a handler that throws is reported as a failure, never as a save", async () => {
    const remove = vi.fn().mockRejectedValue(new Error("network down"));
    const user = openAt("delete", { remove });

    await user.type(screen.getByLabelText(/type .* to confirm/i), "Long term");
    await user.click(screen.getByRole("button", { name: /delete long term/i }));

    const failure = screen.getByTestId("write-failure");
    expect(failure).toHaveTextContent("network down");
    expect(failure).toHaveTextContent("Nothing was changed");
    expect(screen.queryByTestId("manage-confirmation")).not.toBeInTheDocument();
    expect(screen.getByTestId("delete-panel")).toBeInTheDocument();
  });

  it("fails: a transfer refusal names the holding the server complained about", async () => {
    const transfer = vi
      .fn()
      .mockResolvedValue({ ok: false, reason: "ITC is already in Long term." });
    const user = openAt("assign", { transfer });

    await user.click(tick("Infosys"));
    await user.click(screen.getByRole("button", { name: /assign to long term/i }));

    expect(screen.getByTestId("write-failure")).toHaveTextContent("ITC is already in Long term.");
    /* The selection survives, so the person can fix it rather than rebuild it. */
    expect(screen.getByTestId("preview-lines")).toHaveTextContent("INFY");
  });

  it("fails: the host is told only about writes that actually happened", async () => {
    const onChanged = vi.fn();
    const rename = vi.fn().mockResolvedValue({ ok: false, reason: "no" });
    const user = userEvent.setup();
    render(
      <ManagePortfoliosDrawer
        open
        onOpenChange={() => undefined}
        capital={CAPITAL}
        views={VIEWS}
        rows={ROWS}
        initialAction="rename"
        handlers={{ rename }}
        onChanged={onChanged}
      />,
    );

    await user.clear(screen.getByLabelText("New name"));
    await user.type(screen.getByLabelText("New name"), "Swing");
    await user.click(screen.getByRole("button", { name: "Rename" }));
    expect(onChanged).not.toHaveBeenCalled();

    rename.mockResolvedValue({ ok: true, portfolioId: 1 });
    await user.click(screen.getByRole("button", { name: "Rename" }));
    expect(onChanged).toHaveBeenCalledTimes(1);
  });

  it("fails: a control whose save the page never wired is disabled beside its reason", () => {
    openAt("assign");
    /* PC1 removed exactly this defect from the command centre — a primary action wired to a
       callback the page never passed, which swallowed the click and looked live. */
    expect(screen.getByRole("button", { name: /assign to long term/i })).toBeDisabled();
    expect(screen.getByTestId("transfer-blocked-reason")).toBeInTheDocument();
  });
});
