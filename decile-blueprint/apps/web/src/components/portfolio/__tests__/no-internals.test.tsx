import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { CommandCenterScreen } from "@/components/portfolio/command/command-center-screen";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { Unallocated } from "@/lib/portfolio/organize";
import { MONITORING_NOTE, type Overview } from "@/lib/portfolio/overview";

/**
 * Nothing internal reaches a reader's eyes.
 *
 * **This exists because a browser screenshot found what 2,800 unit tests could not.** The brief
 * asks for *"concise, factual and calm"* copy. The screen it got was factual and neither concise
 * nor calm: an empty account rendered 4,661 pixels of explanation on a phone, and the explanations
 * named `GET /api/v1/portfolio/{id}/nav`, `net_flow`, `packages/core`, `todays_contribution` and —
 * worst — a live `http://127.0.0.1:8100` in an error a user would read.
 *
 * The cause was a good rule applied without a reader in mind. "Never a bare dash; always the
 * reason" is right, and the reason a RETAIL INVESTOR needs is "this needs each portfolio's own
 * value history, which this screen does not load" — not the route that would serve it. The
 * engineering detail is not deleted; it moves to the code comment, where the person who can act
 * on it is actually reading.
 *
 * So this test renders the screen and reads it the way a person does — as text — and fails on
 * anything that belongs to the inside of the system.
 */

const BANNED: ReadonlyArray<{ readonly name: string; readonly pattern: RegExp }> = [
  { name: "an API route", pattern: /\b(GET|POST|PATCH|PUT|DELETE)\s+\//i },
  { name: "an API path", pattern: /\/api\/v\d/i },
  { name: "a host and port", pattern: /https?:\/\/[^\s)]+/i },
  { name: "a source file", pattern: /\b[\w/-]+\.(py|ts|tsx|sql)\b/i },
  { name: "a module path", pattern: /\b(packages|services|src)\/[a-z]/i },
  // A column or field name. User-facing English does not contain snake_case.
  { name: "a database or payload field", pattern: /\b[a-z][a-z0-9]*(_[a-z0-9]+)+\b/ },
];

function scan(label: string): void {
  const text = document.body.textContent ?? "";
  for (const { name, pattern } of BANNED) {
    const hit = pattern.exec(text);
    expect(
      hit,
      `${label}: ${name} reached the screen — "${hit?.[0] ?? ""}". Say what a reader needs; put the identifier in the code comment.`,
    ).toBeNull();
  }
}

const EMPTY_PILE: Unallocated = {
  cash: "0",
  cta: "Organize into portfolios",
  holdings_count: 0,
  holdings_value: "0",
  total_value: "0",
  pending_reconciliation: false,
};

function emptyOverview(): Overview {
  return {
    hero: {
      current_value: "0",
      holdings_without_cost_basis: 0,
      pending_reconciliation: false,
      todays_pnl: { label: "today" },
      total_pnl: { label: "total" },
      twr: { label: "Time-weighted return" },
      xirr: { label: "XIRR" },
      secondary: {
        broker_count: 0,
        cash: "0",
        dividends: "0",
        realised_pnl: { label: "realised" },
        unrealised_pnl: { label: "unrealised" },
      },
    },
    chart: {
      pending_reconciliation: false,
      range: "1Y",
      total_return: { label: "Total return", since: null },
    },
    prices_label: "No closing prices yet",
    holdings_synced_label: "Holdings not synced yet",
    portfolios: [],
    monitoring_views: [],
    monitoring_excluded_note: MONITORING_NOTE,
    open_reconciliation_count: 0,
    sync_status: [],
    attention: [],
    unallocated: EMPTY_PILE,
  };
}

describe("nothing internal reaches the reader", () => {
  it("copy: an empty command centre names no route, no column and no file", () => {
    render(
      <TooltipProvider>
        <CommandCenterScreen overview={emptyOverview()} unallocated={EMPTY_PILE} marketOpen={false} />
      </TooltipProvider>,
    );
    expect(screen.getByRole("heading", { name: "Portfolio Command Center" })).toBeInTheDocument();
    scan("the empty command centre");
  });

  it("copy: an unreachable desk explains itself without naming a host", () => {
    /* The worst instance: the regime panel printed the API's own error verbatim, so a reader met
       `http://127.0.0.1:8100/api/v1/desk/regime responded 404`. That is an internal address on a
       screen a customer opens. */
    render(
      <TooltipProvider>
        <CommandCenterScreen
          overview={emptyOverview()}
          unallocated={EMPTY_PILE}
          regime={null}
          /* What the page now hands down — `readerSafeDeskError`, whose own test pins the raw
             transport string it replaces. The panel renders what it is given, so the sanitising
             belongs at the seam where the error is caught, not inside the panel. */
          regimeError="The desk did not answer."
        />
      </TooltipProvider>,
    );
    scan("an unreachable desk");
  });
});
