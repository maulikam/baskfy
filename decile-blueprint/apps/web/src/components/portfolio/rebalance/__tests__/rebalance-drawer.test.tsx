import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { RebalanceDrawer } from "@/components/portfolio/rebalance/rebalance-drawer";
import { SCREENS, detail, rebalance } from "@/components/portfolio/rebalance/__tests__/fixtures";
import { NOT_PRODUCED_HERE } from "@/lib/portfolio/rebalance-preview";
import type { RebalanceDrawerProps } from "@/components/portfolio/rebalance/rebalance-drawer";

/**
 * The rebalance drawer as a person meets it.
 *
 * Written against the brief's own sentences rather than against the markup. The four failures
 * that would matter most in a product that places live orders, and that these hold:
 *
 *   · a quantity, a cash figure or a cost appearing anywhere, derived from weight and price;
 *   · an excluded name silently having its weight re-spread over the names that remain;
 *   · a warning reduced to "1 issue" instead of naming the delisted holding;
 *   · a final step that does anything other than write a document.
 */

afterEach(cleanup);

function draw(over: Partial<RebalanceDrawerProps> = {}) {
  const props: RebalanceDrawerProps = {
    portfolioName: "Momentum 20",
    detail: detail(),
    screens: SCREENS,
    rebalance: rebalance(),
    ...over,
  };
  return render(<RebalanceDrawer {...props} />);
}

async function open(over: Partial<RebalanceDrawerProps> = {}) {
  const user = userEvent.setup();
  draw(over);
  const trigger = screen.getByTestId("open-rebalance-drawer");
  await user.click(trigger);
  const drawer = await screen.findByTestId("rebalance-drawer");
  return { user, drawer, trigger };
}

/** Walk the workflow forwards to a named step, exactly as a reader would. */
async function goTo(user: ReturnType<typeof userEvent.setup>, step: string) {
  await user.click(screen.getByTestId(`step-${step}`));
}

describe("current against target weights", () => {
  it("opens on the four lists the API returns, each named and counted", async () => {
    await open();

    const lists = screen.getByTestId("rebalance-lists");
    expect(within(lists).getByText("Exits")).toBeInTheDocument();
    expect(within(lists).getByText("Entries")).toBeInTheDocument();
    expect(within(lists).getByText("Inside the hold band")).toBeInTheDocument();
    expect(within(lists).getByText("Core holds")).toBeInTheDocument();
    expect(within(screen.getByTestId("list-exit")).getByText("2")).toBeInTheDocument();
    expect(within(screen.getByTestId("list-entry")).getByText("3")).toBeInTheDocument();
  });

  it("shows the current and target weights for every name, and the change between them", async () => {
    const { user } = await open();
    await goTo(user, "adjust");

    const tcs = screen.getByTestId("row-TCS");
    expect(within(tcs).getByText("30.00%")).toBeInTheDocument();
    expect(within(tcs).getByText("16.67%")).toBeInTheDocument();
    expect(within(tcs).getByText("-13.33%")).toBeInTheDocument();

    const entry = screen.getByTestId("row-HDFCBANK");
    expect(within(entry).getByText("0.00%")).toBeInTheDocument();
    expect(within(entry).getByText("+16.67%")).toBeInTheDocument();
  });

  it("renders the reason in place of a missing weight, never a bare dash", async () => {
    const { user, drawer } = await open();
    await goTo(user, "adjust");

    const suzlon = screen.getByTestId("row-SUZLON");
    expect(within(suzlon).getByText("No price today, so it has no weight.")).toBeInTheDocument();
    /* The brief's hard rule: never a "—" without an explanation. Asserted as the rule is meant —
       no element anywhere in the drawer stands there holding only a dash. Prose is allowed its
       em dashes; a CELL is not allowed to be one. */
    for (const node of drawer.querySelectorAll("*")) {
      expect(["—", "–", "-", "N/A", "n/a", "?"]).not.toContain((node.textContent ?? "").trim());
    }
  });

  it("names both accounts a name is held at", async () => {
    const { user } = await open();
    await goTo(user, "adjust");

    expect(within(screen.getByTestId("row-INFY")).getByText(/Upstox, Zerodha/)).toBeInTheDocument();
  });
});

describe("nothing is derived", () => {
  it("shows no quantity, cash figure, turnover or cost on any step — not derived from weight and price", async () => {
    const { user, drawer } = await open();

    for (const step of ["adjust", "impact", "confirm"]) {
      await goTo(user, step);
      const text = drawer.textContent ?? "";
      /* Every money figure in this app renders through `formatRupees`, which always prefixes a
         rupee sign. No rupee sign anywhere means no money figure anywhere. */
      expect(text).not.toContain("₹");
      /* And no share count dressed up as one. */
      expect(text).not.toMatch(/\bBuy\s+\d/i);
      expect(text).not.toMatch(/\bSell\s+\d/i);
      expect(text).not.toMatch(/\d+\s+shares\b/i);
    }

    /* And the artefact the last step writes carries none either. */
    await user.click(screen.getByTestId("acknowledge"));
    await goTo(user, "plan");
    const plan = screen.getByTestId("plan-text").textContent ?? "";
    expect(plan).not.toContain("₹");
    expect(plan).not.toMatch(/\d+\s+shares\b/i);
  });

  it("reports an unknown holding count as unknown, not as zero", async () => {
    const { user } = await open({ detail: null });
    await goTo(user, "impact");

    const counts = screen.getByTestId("name-count");
    expect(counts).toHaveTextContent("Holdings not loaded, so how many names you hold now is unknown");
    expect(counts).not.toHaveTextContent("0 names now");
    expect(screen.getByTestId("largest-now")).toHaveTextContent(
      "Holdings not loaded, so there is no weight to compare.",
    );
  });

  it("names each figure it does not produce, with the reason and where it comes from instead", async () => {
    const { user } = await open();
    await goTo(user, "impact");

    const panel = screen.getByTestId("not-produced-here");
    for (const item of NOT_PRODUCED_HERE) {
      const row = within(panel).getByTestId(`not-produced-${item.id}`);
      expect(within(row).getByText(item.name)).toBeInTheDocument();
      expect(within(row).getByText(item.reason)).toBeInTheDocument();
      expect(within(row).getByText(item.insteadFrom)).toBeInTheDocument();
    }
    expect(within(panel).getByText(/Quantity to buy or sell/)).toBeInTheDocument();
    expect(within(panel).getByText(/Brokerage, STT and charges/)).toBeInTheDocument();
  });

  it("says on the confirm step that the quantities are the reader's to enter, not derived here", async () => {
    const { user } = await open();
    await goTo(user, "confirm");

    const notice = screen.getByTestId("quantity-notice");
    expect(notice).toHaveTextContent("There are no quantities here, and that is deliberate");
    expect(notice).toHaveTextContent(/not derived from weight, value\s+and last price here/);
  });

  it("does not re-spread an excluded name's weight over the names that remain", async () => {
    const { user } = await open();
    await goTo(user, "adjust");
    await user.click(screen.getByTestId("exclude-HDFCBANK"));
    await goTo(user, "impact");

    expect(screen.getByTestId("coverage-figure")).toHaveTextContent("83.33%");
    expect(screen.getByTestId("unassigned-figure")).toHaveTextContent("16.67%");
    await goTo(user, "adjust");
    /* The five that remain keep exactly the weight the server computed. */
    expect(within(screen.getByTestId("row-TCS")).getByText("16.67%")).toBeInTheDocument();
  });
});

describe("the five-step workflow", () => {
  it("is the brief's workflow in its own order, and the last step writes a plan rather than a trade", async () => {
    const { user, drawer } = await open();

    const steps = within(screen.getByTestId("workflow-steps")).getAllByRole("button");
    expect(steps.map((step) => step.textContent)).toEqual([
      "1Analyse",
      "2Adjust",
      "3Impact",
      "4Confirm",
      "5Plan",
    ]);

    await user.click(screen.getByTestId("step-next"));
    expect(screen.getByTestId("weight-comparison")).toBeInTheDocument();
    await user.click(screen.getByTestId("step-next"));
    expect(screen.getByTestId("impact-panel")).toBeInTheDocument();
    await user.click(screen.getByTestId("step-next"));
    expect(screen.getByTestId("confirm-step")).toBeInTheDocument();

    /* The plan does not assemble until the reader has said the quantities are theirs. */
    expect(screen.getByTestId("step-next")).toBeDisabled();
    expect(screen.getByTestId("step-next-blocked")).toHaveTextContent(/Acknowledge/);
    await user.click(screen.getByTestId("acknowledge"));
    await user.click(screen.getByTestId("step-next"));

    const plan = screen.getByTestId("plan-text");
    expect(plan).toHaveTextContent("A PLAN, NOT AN ORDER");
    expect(plan).toHaveTextContent("Baskfy has not sent any of this to a broker");

    /* There is no control anywhere in the drawer that would send anything. */
    const controls = within(drawer).getAllByRole("button");
    for (const control of controls) {
      expect(control.textContent ?? "").not.toMatch(/send|submit|place|execute|buy now|sell now/i);
    }
  });

  it("the workflow carries the reader's note through to the plan", async () => {
    const { user } = await open();
    await goTo(user, "adjust");
    await user.click(screen.getByTestId("add-note-ITC"));
    await user.type(screen.getByTestId("note-ITC"), "half only");

    await goTo(user, "confirm");
    await user.click(screen.getByTestId("acknowledge"));
    await goTo(user, "plan");

    expect(screen.getByTestId("plan-text")).toHaveTextContent("your note: half only");
  });

  it("the workflow asks the parent for a fresh diff rather than fetching one itself", async () => {
    const onAnalyse = vi.fn();
    const { user } = await open({ onAnalyse });

    await user.clear(screen.getByTestId("drawer-top-n"));
    await user.type(screen.getByTestId("drawer-top-n"), "12");
    await user.click(screen.getByTestId("run-diff"));

    expect(onAnalyse).toHaveBeenCalledWith({
      screenPublicId: "scr_momentum",
      topN: 12,
      holdBuffer: 10,
    });
  });
});

describe("exclude and restore", () => {
  it("exclude a name and the comparison updates; put it back and it returns, without reopening the drawer", async () => {
    const { user } = await open();
    await goTo(user, "adjust");

    const before = screen.getByTestId("row-HDFCBANK");
    expect(before).toHaveAttribute("data-excluded", "false");

    await user.click(screen.getByTestId("exclude-HDFCBANK"));
    expect(screen.getByTestId("row-HDFCBANK")).toHaveAttribute("data-excluded", "true");
    expect(screen.getByTestId("exclusion-summary")).toHaveTextContent("1 name excluded");
    expect(screen.getByTestId("exclude-HDFCBANK")).toHaveAttribute("aria-pressed", "true");

    /* The row stays visible while excluded — a name that vanishes cannot be put back. */
    expect(screen.getByTestId("row-HDFCBANK")).toHaveTextContent("Put HDFCBANK back");

    await user.click(screen.getByTestId("exclude-HDFCBANK"));
    expect(screen.getByTestId("row-HDFCBANK")).toHaveAttribute("data-excluded", "false");
    expect(screen.getByTestId("exclusion-summary")).toHaveTextContent("Nothing is excluded");
    /* The drawer never closed. */
    expect(screen.getByTestId("rebalance-drawer")).toBeInTheDocument();
  });

  it("restores every excluded name at once", async () => {
    const { user } = await open();
    await goTo(user, "adjust");

    await user.click(screen.getByTestId("exclude-HDFCBANK"));
    await user.click(screen.getByTestId("exclude-DHFL"));
    expect(screen.getByTestId("exclusion-summary")).toHaveTextContent("2 names excluded");

    await user.click(screen.getByTestId("restore-all"));
    expect(screen.getByTestId("exclusion-summary")).toHaveTextContent("Nothing is excluded");
  });

  it("offers no exclude control on a name that proposes no action", async () => {
    const { user } = await open();
    await goTo(user, "adjust");

    expect(screen.queryByTestId("exclude-TCS")).toBeNull();
    expect(screen.getByTestId("row-TCS")).toHaveTextContent("No action to exclude");
  });

  it("keeping an exit makes the after-figures unavailable, naming the name that did it", async () => {
    const { user } = await open();
    await goTo(user, "adjust");
    await user.click(screen.getByTestId("exclude-DHFL"));
    await goTo(user, "impact");

    const after = screen.getByTestId("largest-after");
    expect(after).toHaveTextContent("DHFL");
    expect(after).toHaveTextContent("no longer add to 100%");
    expect(after).not.toHaveTextContent("%%");
  });

  it("an exclusion withdraws an acknowledgement already given", async () => {
    const { user } = await open();
    await goTo(user, "confirm");
    await user.click(screen.getByTestId("acknowledge"));
    expect(screen.getByTestId("step-next")).toBeEnabled();

    await goTo(user, "adjust");
    await user.click(screen.getByTestId("exclude-ITC"));
    await goTo(user, "confirm");

    expect(screen.getByTestId("acknowledge")).not.toBeChecked();
  });
});

describe("warnings", () => {
  it("warns by name: the delisted holding and the unpriced one are named, not counted", async () => {
    const { user } = await open();
    await goTo(user, "impact");

    const warnings = screen.getByTestId("preview-warnings");
    expect(within(warnings).getByTestId("warning-delisted")).toHaveTextContent("DHFL is delisted");
    expect(within(warnings).getByTestId("warning-unpriced")).toHaveTextContent(
      "SUZLON has no price today",
    );
    /* Severity is a word as well as a colour and an icon. */
    expect(within(warnings).getByTestId("warning-delisted")).toHaveTextContent("Critical");
  });

  it("warns when the screen and the holdings are marked at different sessions", async () => {
    const { user } = await open({ detail: detail({ prices_as_of: "2026-09-11" }) });
    await goTo(user, "impact");

    expect(screen.getByTestId("warning-stale-as-of")).toHaveTextContent(
      "The screen ran on 2026-09-10; your holdings are marked at 2026-09-11",
    );
  });

  it("warns that the book does not reconcile, and stays specific about the cost", async () => {
    const { user } = await open({ detail: detail({ pending_reconciliation: true }) });
    await goTo(user, "impact");

    expect(screen.getByTestId("warning-reconciliation")).toHaveTextContent(
      "Baskfy and the broker disagree about what is held",
    );
  });

  it("says plainly when there is nothing to warn about", async () => {
    const clean = detail();
    const priced = { ...clean, holdings: (clean.holdings ?? []).filter((row) => row.weight !== null) };
    const { user } = await open({
      detail: priced,
      rebalance: rebalance({ exits: [], delisted_count: 0, holdings_count: 4 }),
    });
    await goTo(user, "impact");

    expect(screen.getByTestId("preview-warnings")).toHaveTextContent(
      "Nothing is delisted, every holding has a price",
    );
  });

  it("never phrases a warning as investment advice", async () => {
    const { user, drawer } = await open({ detail: detail({ pending_reconciliation: true }) });
    await goTo(user, "impact");

    const text = drawer.textContent ?? "";
    for (const phrase of [
      "you should buy",
      "you should sell",
      "we recommend",
      "recommended buy",
      "good time to",
      "strong buy",
    ]) {
      expect(text.toLowerCase()).not.toContain(phrase);
    }
  });
});

describe("blocked and empty states", () => {
  it("a portfolio with no screen behind it opens in a blocked state with one next action", async () => {
    await open({ screens: [] });

    const blocked = screen.getByTestId("no-screen-state");
    expect(blocked).toHaveTextContent("There is no screen to rebalance against");
    expect(blocked).toHaveTextContent("no target allocation stored");
    expect(screen.getByTestId("no-screen-action")).toHaveAttribute("href", "/build");
    /* Not an empty shell: the workflow and the comparison are absent, not blank. */
    expect(screen.queryByTestId("workflow-steps")).toBeNull();
    expect(screen.queryByTestId("weight-comparison")).toBeNull();
  });

  it("before a diff has run, the later steps are unreachable and the state says why", async () => {
    await open({ rebalance: null });

    expect(screen.getByTestId("not-analysed-state")).toHaveTextContent("Nothing has been compared yet");
    expect(screen.getByTestId("step-adjust")).toBeDisabled();
    expect(screen.getByTestId("step-plan")).toBeDisabled();
    expect(screen.getByTestId("step-next-blocked")).toHaveTextContent("Run the diff first");
  });

  it("degrades to a stated reason when the holdings payload was never loaded", async () => {
    const { user } = await open({ detail: null });
    await goTo(user, "adjust");

    expect(within(screen.getByTestId("row-TCS")).getByText(/Holdings not loaded/)).toBeInTheDocument();
  });

  it("a disabled action states what would enable it, rather than swallowing the click", async () => {
    await open();

    /* No `onAnalyse` was passed, so the primary control here cannot do anything — and says so.
       PC1 shipped exactly this defect once and it reached a user. */
    expect(screen.getByTestId("run-diff")).toBeDisabled();
    expect(screen.getByTestId("cannot-run")).toHaveTextContent(
      "opened without a way to re-run the diff",
    );
  });

  it("surfaces the server's own words when a diff fails", async () => {
    await open({ rebalance: null, analyseError: "That screen has no result for 2026-09-10." });

    expect(screen.getByTestId("analyse-error")).toHaveTextContent(
      "That screen has no result for 2026-09-10.",
    );
  });
});

describe("keyboard and focus", () => {
  it("keyboard: the drawer is a dialog, traps focus, closes on Escape and gives focus back to its trigger", async () => {
    const { user, drawer, trigger } = await open();

    expect(drawer).toHaveAttribute("role", "dialog");
    expect(drawer).toHaveAttribute("aria-modal", "true");
    expect(drawer).toHaveAccessibleName(/Review rebalance/);
    /* The modality is real, not only asserted: everything outside the portal is hidden. */
    expect(trigger.closest("[aria-hidden='true']")).not.toBeNull();

    await waitFor(() => expect(drawer.contains(document.activeElement)).toBe(true));

    /* Round the whole tab order several times. Focus must never land outside the drawer. */
    for (let press = 0; press < 24; press += 1) {
      await user.tab();
      expect(drawer.contains(document.activeElement)).toBe(true);
    }

    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByTestId("rebalance-drawer")).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  it("keyboard: every step is reachable without a pointer", async () => {
    const { user } = await open();

    screen.getByTestId("step-adjust").focus();
    await user.keyboard("{Enter}");
    expect(screen.getByTestId("weight-comparison")).toBeInTheDocument();

    screen.getByTestId("exclude-HDFCBANK").focus();
    await user.keyboard("{Enter}");
    expect(screen.getByTestId("row-HDFCBANK")).toHaveAttribute("data-excluded", "true");
  });
});
