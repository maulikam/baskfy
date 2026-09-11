import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  ManagePortfoliosDrawer,
  type ManageHandlers,
} from "@/components/portfolio/manage/manage-drawer";
import { BROKERS, CAPITAL, ROWS, VIEWS } from "@/components/portfolio/manage/__tests__/fixtures";
import type { ManageActionId, SleeveRow } from "@/lib/portfolio/manage";

/**
 * Sub-portfolios and connections — the two panels whose danger is not obvious from the form.
 *
 * `PUT /portfolios/{id}/sleeves` replaces the *whole* division, so "add a sleeve" is really
 * "send the set I believe is there, plus one". A form that has not read the set is a form that
 * deletes it. And re-attributing a broker account relabels the container while moving no share,
 * which is the opposite of what the words "broker account" suggest on a portfolio form.
 */

const SLEEVES: readonly SleeveRow[] = [
  { id: 1, name: "Core", kind: "manual", capital: "300000.00", top_n: 15 },
  { id: 2, name: "Satellite", kind: "screen", capital: "100000.00", top_n: 20, screen_name: "Momentum 50" },
];

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
      openReconciliationCount={2}
      initialAction={panel}
      handlers={handlers}
    />,
  );
  return user;
}

describe("sub-portfolios", () => {
  it("sub-portfolios: the save stays off until the current split has been read back", () => {
    /* No `loadSleeves` handler at all. Saving now would send one sleeve as the whole division. */
    openAt("sleeve", { saveSleeves: vi.fn() });
    expect(screen.getByTestId("sleeve-not-wired")).toHaveTextContent(
      "would delete the allocations already saved",
    );
    expect(screen.getByRole("button", { name: /add this allocation/i })).toBeDisabled();
  });

  it("sub-portfolios: a failed read refuses to save rather than replacing what it cannot see", async () => {
    const loadSleeves = vi.fn().mockRejectedValue(new Error("the division could not be fetched"));
    openAt("sleeve", { loadSleeves, saveSleeves: vi.fn() });

    const warning = await screen.findByTestId("sleeve-load-failed");
    expect(warning).toHaveTextContent("replaces the whole set");
    expect(warning).toHaveTextContent("the division could not be fetched");
    expect(screen.getByRole("button", { name: /add this allocation/i })).toBeDisabled();
    expect(screen.getByTestId("sleeve-blocked-reason")).toHaveTextContent(
      "has to be read before it can be replaced",
    );
  });

  it("sub-portfolios: it shows the split it read, and says saving replaces all of it", async () => {
    const loadSleeves = vi.fn().mockResolvedValue(SLEEVES);
    openAt("sleeve", { loadSleeves, saveSleeves: vi.fn() });

    const current = await screen.findByTestId("sleeve-current");
    expect(current).toHaveTextContent("is split into 2 allocations");
    expect(current).toHaveTextContent("Core");
    expect(current).toHaveTextContent("3,00,000");
    expect(current).toHaveTextContent("Satellite");
    expect(screen.getByTestId("sleeve-replace-note")).toHaveTextContent(
      "sent back unchanged alongside the new one",
    );
  });

  it("sub-portfolios: adding one sends the allocations already there, not just the new one", async () => {
    const loadSleeves = vi.fn().mockResolvedValue(SLEEVES);
    const saveSleeves = vi.fn().mockResolvedValue({ ok: true, portfolioId: 1 });
    const user = openAt("sleeve", { loadSleeves, saveSleeves });

    await screen.findByTestId("sleeve-current");
    await user.type(screen.getByLabelText("New allocation"), "Momentum");
    await user.type(screen.getByLabelText("Capital"), "400000");

    expect(screen.getByTestId("sleeve-preview")).toHaveTextContent(
      "Capital given out goes from ₹4,00,000 to ₹8,00,000.",
    );

    await user.click(screen.getByRole("button", { name: /add this allocation/i }));

    expect(saveSleeves).toHaveBeenCalledTimes(1);
    const [portfolioId, body] = saveSleeves.mock.calls[0] as [number, readonly { name: string }[]];
    expect(portfolioId).toBe(1);
    expect(body.map((sleeve) => sleeve.name)).toEqual(["Core", "Satellite", "Momentum"]);
  });
});

describe("connections", () => {
  it("connections: re-attributing says outright that it moves no share", () => {
    openAt("broker", { reattribute: vi.fn() });
    const notice = screen.getByTestId("reattribute-notice");
    expect(notice).toHaveTextContent("relabels the container and moves no share");
    expect(notice).toHaveTextContent("declaration conflict");
  });

  it("connections: it will not re-attribute until an account is chosen, because it cannot show the current one", () => {
    /* `PortfolioRowOut` carries the brokers whose holdings are in a portfolio, not the account it
       is declared against. A select that defaulted to an option would let one click silently
       change a declaration nobody had looked at. */
    openAt("broker", { reattribute: vi.fn() });
    expect(screen.getByRole("button", { name: "Re-attribute" })).toBeDisabled();
    expect(screen.getByTestId("broker-unchosen")).toBeInTheDocument();
    expect(screen.getByTestId("connections-panel")).toHaveTextContent(
      "does not carry the account a portfolio is currently declared against",
    );
  });

  it("connections: re-attribution sends the portfolio and the account, and null for a span", async () => {
    const reattribute = vi.fn().mockResolvedValue({ ok: true, portfolioId: 1 });
    const user = openAt("broker", { reattribute });

    await user.selectOptions(screen.getByLabelText("Attributed to"), "12");
    await user.click(screen.getByRole("button", { name: "Re-attribute" }));
    expect(reattribute).toHaveBeenLastCalledWith(1, 12);

    await user.selectOptions(screen.getByLabelText("Attributed to"), "");
    await user.click(screen.getByRole("button", { name: "Re-attribute" }));
    expect(reattribute).toHaveBeenLastCalledWith(1, null);
  });

  it("connections: there are no reconciliation settings, and the panel says so instead of inventing one", () => {
    openAt("broker");
    expect(screen.getByTestId("reconciliation-no-settings")).toHaveTextContent(
      "no tolerance, no auto-resolve rule",
    );
    expect(screen.getByTestId("reconciliation-count")).toHaveTextContent("2 questions are open");
    expect(screen.getByRole("link", { name: /reconciliation inbox/i })).toHaveAttribute(
      "href",
      "/reconcile",
    );
  });

  it("connections: connecting a broker links out rather than half-drawing an OAuth flow", () => {
    openAt("broker");
    expect(screen.getByRole("link", { name: /broker connections/i })).toHaveAttribute(
      "href",
      "/brokers",
    );
    expect(screen.getByTestId("connections-panel")).toHaveTextContent("2 accounts connected");
  });
});
