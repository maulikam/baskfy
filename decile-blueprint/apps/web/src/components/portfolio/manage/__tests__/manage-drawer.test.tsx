import { useState } from "react";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import {
  ManagePortfoliosDrawer,
  type ManagePortfoliosDrawerProps,
} from "@/components/portfolio/manage/manage-drawer";
import { BROKERS, CAPITAL, ROWS, VIEWS } from "@/components/portfolio/manage/__tests__/fixtures";
import { MANAGE_ACTIONS, unavailableActions } from "@/lib/portfolio/manage";

/**
 * The manage drawer as a person meets it.
 *
 * Written against the brief's sentences rather than the markup, because the markup is the part
 * that is allowed to change. The three that would each be a real defect:
 *
 *   · a control for something Baskfy cannot do — the greyed-out button that teaches a person the
 *     product is broken;
 *   · a governance feature that is simply *missing*, so a reader cannot tell whether it is absent
 *     or whether they failed to find it;
 *   · a dialog a keyboard cannot leave, or one that drops focus into the void on close.
 */

type HostProps = Partial<Omit<ManagePortfoliosDrawerProps, "open" | "onOpenChange">>;

/** A host with a real trigger, because "focus returns to its trigger" needs one to return to. */
function Host(props: HostProps = {}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Manage portfolios
      </button>
      <ManagePortfoliosDrawer
        open={open}
        onOpenChange={setOpen}
        capital={CAPITAL}
        views={VIEWS}
        rows={ROWS}
        brokers={BROKERS}
        openReconciliationCount={2}
        {...props}
      />
    </>
  );
}

async function openDrawer(props: HostProps = {}) {
  const user = userEvent.setup();
  render(<Host {...props} />);
  const trigger = screen.getByRole("button", { name: "Manage portfolios" });
  await user.click(trigger);
  return { user, trigger };
}

describe("the manage drawer index", () => {
  it("actions: offers create, rename, assign, move, sub-portfolios, brokers and delete", async () => {
    await openDrawer();
    for (const id of ["create", "rename", "assign", "move", "watch", "sleeve", "broker", "delete"]) {
      expect(screen.getByTestId(`manage-action-${id}`)).toBeInTheDocument();
    }
  });

  it("actions: every control it draws names the endpoint behind it", async () => {
    await openDrawer();
    /* Not decoration. The whole reason the catalogue carries a typed endpoint is that a control
       with nothing behind it is the defect this leaf exists to prevent, and `data-endpoint` is
       the seam where a button and a route are asserted to be the same decision. */
    for (const id of ["create", "rename", "assign", "move", "watch", "sleeve", "broker", "delete"]) {
      const endpoint = screen.getByTestId(`manage-action-${id}`).getAttribute("data-endpoint");
      expect(endpoint).toMatch(/^(GET|POST|PUT|PATCH|DELETE) \/api\/v1\//);
    }
  });

  it("actions: opening one shows its panel, and there is a way back to the list", async () => {
    const { user } = await openDrawer();
    await user.click(screen.getByTestId("manage-action-rename"));
    expect(screen.getByTestId("rename-panel")).toBeInTheDocument();
    expect(screen.queryByTestId("manage-index")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /all actions/i }));
    expect(screen.getByTestId("manage-index")).toBeInTheDocument();
  });

  it("unavailable: archive, permissions, ownership, audit and objective are named, not drawn", async () => {
    await openDrawer();
    const section = screen.getByTestId("manage-unavailable");
    for (const id of ["archive", "permissions", "ownership", "audit", "objective"]) {
      expect(within(section).getByTestId(`unavailable-${id}`)).toBeInTheDocument();
    }
    /* The point of the section: NOTHING in it can be pressed. A disabled button would still be a
       control — it takes the place a working one would and invites the click. */
    expect(within(section).queryAllByRole("button")).toHaveLength(0);
    expect(within(section).queryAllByRole("link")).toHaveLength(0);
    expect(within(section).queryAllByRole("textbox")).toHaveLength(0);
  });

  it("unavailable: each one says what it would do, why it cannot, and what would unblock it", async () => {
    await openDrawer();
    for (const action of unavailableActions()) {
      const entry = screen.getByTestId(`unavailable-${action.id}`);
      expect(entry).toHaveTextContent(action.title);
      if (action.availability.kind !== "unavailable") continue;
      expect(entry).toHaveTextContent("Why:");
      expect(entry).toHaveTextContent("What would unblock it:");
      /* The state is a word as well as an icon, so it survives greyscale (§6.2 rule 3). */
      expect(entry).toHaveTextContent(/not available yet/i);
    }
  });

  it("unavailable: the four governance features are attributed to the multi-tenant work", async () => {
    await openDrawer();
    for (const id of ["permissions", "ownership"]) {
      expect(screen.getByTestId(`unavailable-${id}`)).toHaveTextContent(/C3 multi-tenant/i);
    }
    expect(screen.getByTestId("unavailable-audit")).toHaveTextContent(/audit table/i);
    expect(screen.getByTestId("unavailable-archive")).toHaveTextContent(/archived/i);
  });

  it("actions: the drawer never phrases anything as investment advice", async () => {
    await openDrawer();
    const text = (screen.getByTestId("manage-drawer").textContent ?? "").toLowerCase();
    for (const phrase of ["you should buy", "you should sell", "we recommend", "book profit"]) {
      expect(text).not.toContain(phrase);
    }
    /* And it says outright that nothing here reaches a broker. */
    expect(text).toContain("nothing here places an order");
  });

  it("actions: every catalogue entry is either drawn as a control or named as unavailable", async () => {
    const { user } = await openDrawer();
    for (const action of MANAGE_ACTIONS) {
      const drawn = screen.queryByTestId(`manage-action-${action.id}`) !== null;
      const named = screen.queryByTestId(`unavailable-${action.id}`) !== null;
      if (drawn || named) continue;
      /* The two that live inside a form rather than on the index, because that is where a person
         goes looking for them. Asserted rather than excused. */
      expect(["benchmark", "reconciliation"]).toContain(action.id);
    }
    await user.click(screen.getByTestId("manage-action-rename"));
    expect(screen.getByTestId("rename-absent-benchmark")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /all actions/i }));
    await user.click(screen.getByTestId("manage-action-broker"));
    expect(screen.getByTestId("reconciliation-no-settings")).toBeInTheDocument();
  });
});

describe("the manage drawer and the keyboard", () => {
  it("keyboard: it is announced as a dialog with a name, and focus starts inside it", async () => {
    await openDrawer();
    const dialog = screen.getByRole("dialog", { name: /manage portfolios/i });
    expect(dialog).toBeInTheDocument();
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog.contains(document.activeElement)).toBe(true);
  });

  it("keyboard: tabbing cannot leave the drawer while it is open", async () => {
    const { user, trigger } = await openDrawer();
    const dialog = screen.getByRole("dialog");
    for (let step = 0; step < 12; step += 1) {
      await user.tab();
      expect(dialog.contains(document.activeElement)).toBe(true);
      expect(document.activeElement).not.toBe(trigger);
    }
  });

  it("keyboard: Escape closes it and focus returns to the trigger that opened it", async () => {
    const { user, trigger } = await openDrawer();
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    await user.keyboard("{Escape}");

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    /* A dialog that closes and drops focus on `document.body` leaves a keyboard user at the top
       of the page with no idea where they were. */
    expect(trigger).toHaveFocus();
  });

  it("keyboard: re-opening it starts back at the list rather than the last panel used", async () => {
    const { user, trigger } = await openDrawer();
    await user.click(screen.getByTestId("manage-action-delete"));
    expect(screen.getByTestId("delete-panel")).toBeInTheDocument();

    await user.keyboard("{Escape}");
    await user.click(trigger);

    /* Reopening on a destructive panel somebody merely glanced at is how a delete happens by
       muscle memory. */
    expect(screen.getByTestId("manage-index")).toBeInTheDocument();
    expect(screen.queryByTestId("delete-panel")).not.toBeInTheDocument();
  });
});
