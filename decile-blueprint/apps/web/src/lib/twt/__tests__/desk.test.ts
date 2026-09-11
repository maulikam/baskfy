import { describe, expect, it } from "vitest";

import { deskPlan, planLine } from "@/lib/twt/__tests__/fixtures";
import { deskView, skipWords } from "@/lib/twt/desk";

/**
 * The twt desk view model — `docs/twt/05` §2, without a browser.
 *
 * Expiry is the rule on that page most worth a failing test, and it is only testable because the
 * clock is a parameter. A view model that read `Date.now()` internally would make "what happens
 * at minute 31" a thing nobody can write a failing test for.
 */

const AT_TEN_PAST = new Date("2026-09-10T18:10:00+05:30");

describe("the twt desk plan expires on the clock it was given", () => {
  it("is live with minutes left inside the window", () => {
    const view = deskView(deskPlan(), AT_TEN_PAST);
    expect(view.expired).toBe(false);
    expect(view.minutesLeft).toBe(20);
  });

  it("expired: every line stops being confirmable the moment the window closes", () => {
    const exactly = deskView(deskPlan(), new Date("2026-09-10T18:30:00+05:30"));
    expect(exactly.expired).toBe(true);
    expect(exactly.minutesLeft).toBe(0);
    for (const section of exactly.sections) {
      for (const line of section.lines) expect(line.confirmable).toBe(false);
    }
  });

  /* A malformed timestamp must fail closed. Treating it as live would make a bad clock the one
     way to get a confirm control onto a plan nobody vouched for. */
  it("expired: an unreadable expiry is treated as expired, never as live", () => {
    expect(deskView(deskPlan({ expires_at: "" }), AT_TEN_PAST).expired).toBe(true);
  });

  it("keeps a line that has already been sent out of the confirmable set", () => {
    const view = deskView(deskPlan({ lines: [planLine({ state: "SENT" })] }), AT_TEN_PAST);
    expect(view.sections[1]?.lines[0]?.confirmable).toBe(false);
  });
});

describe("the twt desk plan orders the session the way the morning should be worked", () => {
  it("returns the exits section before the entries section", () => {
    const view = deskView(deskPlan(), AT_TEN_PAST);
    expect(view.sections.map((section) => section.id)).toEqual(["exits", "entries"]);
  });

  it("puts a missing stop ahead of a stop that only needs raising", () => {
    const view = deskView(deskPlan(), AT_TEN_PAST);
    expect(view.sections[0]?.lines.map((line) => line.kind)).toEqual([
      "ARM_GTT",
      "RAISE_GTT_STOP",
    ]);
  });

  it("says why an empty entries section is empty, and the reason depends on the gate", () => {
    const shut = deskView(deskPlan({ gate: "SHUT", lines: [] }), AT_TEN_PAST);
    expect(shut.sections[1]?.emptyReason).toMatch(/gate is shut/i);
    const open = deskView(deskPlan({ gate: "OPEN", lines: [] }), AT_TEN_PAST);
    expect(open.sections[1]?.emptyReason).toMatch(/No name signalled an entry/i);
  });

  it("turns every skip reason into a sentence a person can act on", () => {
    expect(skipWords("SLOTS_FULL")).toMatch(/every position slot is taken/i);
    expect(skipWords("NO_SLEEVE_CAPITAL")).toMatch(/no money has been set aside/i);
    expect(skipWords("LOCKED_UPPER_CIRCUIT")).toMatch(/upper circuit/i);
    /* None of them may be the stored spelling itself. */
    expect(skipWords("BELOW_MIN_TRADE_VALUE")).not.toMatch(/_/);
  });

  it("carries the unprotected lines the afternoon sweep found", () => {
    expect(deskView(deskPlan(), AT_TEN_PAST).sweepNaked).toEqual(["NAKEDCO"]);
    expect(
      deskView(deskPlan({ sweep: { at: null, naked: [] } }), AT_TEN_PAST).sweepNaked,
    ).toEqual([]);
  });

  it("shortens the plan's reference rather than printing the whole of it", () => {
    expect(deskView(deskPlan(), AT_TEN_PAST).planReference).toBe("7f3c1a2b");
  });
});
