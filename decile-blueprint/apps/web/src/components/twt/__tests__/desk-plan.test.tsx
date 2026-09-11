import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { DeskPlan } from "@/components/twt/desk/desk-plan";
import { deskPlan, planLine } from "@/lib/twt/__tests__/fixtures";
import { deskView, type DeskLineView } from "@/lib/twt/desk";

/**
 * `docs/twt/05` §2 — the operator's page, and the four rules of it that are actually rules.
 *
 * The component is pure and the clock is a parameter, so expiry is a fact a test can state rather
 * than a wall-clock race. That is the whole reason `deskView` takes a `now`.
 */

const BEFORE_EXPIRY = new Date("2026-09-10T18:10:00+05:30");
const AFTER_EXPIRY = new Date("2026-09-10T18:45:00+05:30");

function confirm(line: DeskLineView) {
  return <button type="button">Confirm {line.symbol}</button>;
}

describe("the twt desk page puts the stops before the buying", () => {
  /**
   * `05` §2: exits first, "for the reason the swing desk puts them first: a morning that runs out
   * of attention should have armed the stops". Asserted by document position, because a section
   * that exists in the markup in the wrong order satisfies every presence check and still fails.
   */
  it("renders the exits above the entries, and the open positions below both", () => {
    render(<DeskPlan view={deskView(deskPlan(), BEFORE_EXPIRY)} />);

    const exits = screen.getByTestId("twt-desk-exits");
    const entries = screen.getByTestId("twt-desk-entries");
    const positions = screen.getByTestId("twt-desk-positions");
    expect(exits.compareDocumentPosition(entries)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(entries.compareDocumentPosition(positions)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
  });

  /**
   * Within the exits, arming a missing stop outranks raising an existing one: a line with nothing
   * resting is unprotected right now; a line whose stop is merely low is protected at yesterday's
   * level.
   */
  it("puts a missing stop above a stop that only needs raising", () => {
    render(<DeskPlan view={deskView(deskPlan(), BEFORE_EXPIRY)} />);

    const lines = screen
      .getByTestId("twt-desk-exits")
      .querySelectorAll("[data-testid='twt-desk-line']");
    expect(lines[0]?.getAttribute("data-line-kind")).toBe("ARM_GTT");
    expect(lines[1]?.getAttribute("data-line-kind")).toBe("RAISE_GTT_STOP");
  });

  it("shows the mode as a badge, not as a footnote", () => {
    render(<DeskPlan view={deskView(deskPlan(), BEFORE_EXPIRY)} />);

    const badge = screen.getByTestId("twt-desk-mode");
    expect(badge).toHaveTextContent("DRY RUN");
    /* A badge: a coloured chip in the strip at the top, in the same row as the gate and the date.
       `05` §2.1 says so in as many words, because the desk console's own history is of the mode
       being a line of small print somebody stopped reading after the first week. */
    expect(screen.getByTestId("twt-desk-strip").contains(badge)).toBe(true);
  });

  it("carries the counts that say whether the session's own mechanism ran", () => {
    render(<DeskPlan view={deskView(deskPlan(), BEFORE_EXPIRY)} />);

    const strip = screen.getByTestId("twt-desk-strip");
    expect(strip).toHaveTextContent("Stops raised");
    expect(strip).toHaveTextContent("4");
    expect(strip).toHaveTextContent("Gate OPEN");
  });

  it("says why each name was passed over, in words rather than in its stored tag", () => {
    render(<DeskPlan view={deskView(deskPlan(), BEFORE_EXPIRY)} />);

    const skips = screen.getByTestId("twt-desk-skips");
    expect(skips).toHaveTextContent("SKIPCO");
    expect(skips).toHaveTextContent(/not enough trades through it on an average day/i);
  });
});

describe("the twt desk page removes the controls on an expired plan", () => {
  it("offers a confirm control on every proposed line while the plan is live", () => {
    render(
      <DeskPlan view={deskView(deskPlan(), BEFORE_EXPIRY)} renderConfirm={confirm} />,
    );

    expect(screen.getAllByRole("button", { name: /^Confirm / })).toHaveLength(3);
    expect(screen.getByTestId("twt-plan-live")).toHaveTextContent(/minutes left/i);
  });

  /**
   * **The rule most worth a failing test.** An expired plan's buttons are absent, not disabled.
   *
   * A greyed-out Confirm still reads as a button that could work, so it invites a reload and a
   * retry — and a person retrying an expired plan is a person looking for a way to send it
   * anyway. So this asserts the *absence* of the control rather than the presence of a `disabled`
   * attribute: asserting `disabled` would pass on exactly the implementation the rule forbids.
   */
  it("an expired plan has no confirm control at all — not a disabled one", () => {
    const { container } = render(
      <DeskPlan view={deskView(deskPlan(), AFTER_EXPIRY)} renderConfirm={confirm} />,
    );

    expect(screen.queryAllByRole("button", { name: /^Confirm / })).toHaveLength(0);
    expect(container.querySelectorAll("button[disabled]")).toHaveLength(0);
    expect(container.querySelectorAll("[aria-disabled='true']")).toHaveLength(0);
    expect(screen.getByTestId("twt-plan-expired")).toHaveTextContent(
      /This plan has expired\. Rebuild it/i,
    );
  });

  it("treats an unreadable expiry as expired rather than as live", () => {
    render(
      <DeskPlan
        view={deskView(deskPlan({ expires_at: "not a time" }), BEFORE_EXPIRY)}
        renderConfirm={confirm}
      />,
    );

    expect(screen.queryAllByRole("button", { name: /^Confirm / })).toHaveLength(0);
    expect(screen.getByTestId("twt-plan-expired")).toBeInTheDocument();
  });

  it("says what happened to a line already sent instead of offering it again", () => {
    render(
      <DeskPlan
        view={deskView(
          deskPlan({ lines: [planLine({ state: "FILLED" })] }),
          BEFORE_EXPIRY,
        )}
        renderConfirm={confirm}
      />,
    );

    expect(screen.queryAllByRole("button", { name: /^Confirm / })).toHaveLength(0);
    expect(screen.getByTestId("twt-line-state")).toHaveTextContent("filled");
  });
});

describe("the twt desk page carries the afternoon sweep until it is clean", () => {
  it("names every open line with no stop resting, in a band", () => {
    render(<DeskPlan view={deskView(deskPlan(), BEFORE_EXPIRY)} />);

    const band = screen.getByTestId("twt-sweep-band");
    expect(band).toHaveTextContent("NAKEDCO");
    expect(band).toHaveTextContent(/no stop resting at the exchange/i);
    expect(band).toHaveTextContent(/stays until every one of them is protected/i);
  });

  it("shows no band once the sweep comes back clean", () => {
    render(
      <DeskPlan
        view={deskView(deskPlan({ sweep: { at: null, naked: [] } }), BEFORE_EXPIRY)}
      />,
    );

    expect(screen.queryByTestId("twt-sweep-band")).not.toBeInTheDocument();
  });

  it("offers one re-arm per unprotected position and none anywhere else", () => {
    render(
      <DeskPlan
        view={deskView(deskPlan(), BEFORE_EXPIRY)}
        renderRearm={(position) => (
          <button type="button">Put a stop back on {position.symbol}</button>
        )}
      />,
    );

    const buttons = screen.getAllByRole("button", { name: /^Put a stop back on/ });
    expect(buttons).toHaveLength(1);
    expect(buttons[0]).toHaveTextContent("NAKEDCO");
  });
});

describe("the twt desk page states an empty plan rather than rendering nothing", () => {
  it("state: a shut gate says no entry was planned, and still lists the stops", () => {
    render(
      <DeskPlan
        view={deskView(
          deskPlan({
            gate: "SHUT",
            lines: [planLine({ id: 9, kind: "RAISE_GTT_STOP", symbol: "HOLDCO" })],
          }),
          BEFORE_EXPIRY,
        )}
      />,
    );

    expect(screen.getByTestId("twt-desk-entries")).toHaveTextContent(
      /The gate is shut for this session, so no entry was planned/i,
    );
    expect(screen.getByTestId("twt-desk-exits")).toHaveTextContent("HOLDCO");
  });

  it("state: an empty exits section says the stops are already where they should be", () => {
    render(
      <DeskPlan
        view={deskView(deskPlan({ lines: [planLine()] }), BEFORE_EXPIRY)}
      />,
    );

    expect(screen.getByTestId("twt-desk-exits")).toHaveTextContent(
      /already has the right stop resting at the exchange/i,
    );
  });
});
