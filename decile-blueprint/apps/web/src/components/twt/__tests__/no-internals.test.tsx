import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { BacktestCard } from "@/components/twt/backtest-card";
import { DeskPlan } from "@/components/twt/desk/desk-plan";
import { GateCard } from "@/components/twt/gate-card";
import { HalfSizeCounter } from "@/components/twt/half-size-counter";
import { OpenPositions } from "@/components/twt/open-positions";
import { TightNames } from "@/components/twt/tight-names";
import {
  backtest,
  deskPlan,
  emptyToday,
  gate,
  halfSize,
  nakedPosition,
  today,
} from "@/lib/twt/__tests__/fixtures";
import { deskView } from "@/lib/twt/desk";
import { todayView } from "@/lib/twt/view";

/**
 * The two rules the portfolio work established on 11 Sep 2026, applied to `/twt` — TW8 gate 5.
 *
 * **1. Nothing internal reaches the reader.** A browser screenshot found what 2,800 unit tests
 * could not: a customer-facing page carrying `http://127.0.0.1:8100/api/v1/desk/regime responded
 * 404`, plus `net_flow`, `packages/core` and `todays_contribution` in explanatory text — and six
 * tests were *pinning* that defect. The cause was a good rule applied without a reader in mind.
 * "Never a bare dash; always the reason" is right, and the reason a person needs is "no live
 * price has come through for this name yet", not the route that would have served one. The
 * engineering detail is not deleted; it moves to the code comment, where the person who can act
 * on it is reading.
 *
 * **2. Never a bare dash.** A dash is the worst of both answers: it neither gives the figure nor
 * admits the figure is missing, so the reader supplies their own explanation. On a page about
 * resting stops, the explanation they supply may be "there is no risk here".
 *
 * So this renders every surface of `/twt` — including the states with no data at all, which is
 * where both defects live — and reads them the way a person does, as text.
 */

const BANNED: ReadonlyArray<{ readonly name: string; readonly pattern: RegExp }> = [
  { name: "an API route", pattern: /\b(GET|POST|PATCH|PUT|DELETE)\s+\//i },
  { name: "an API path", pattern: /\/api\/v\d/i },
  { name: "a host and port", pattern: /https?:\/\/[^\s)]+/i },
  { name: "a source file", pattern: /\b[\w/-]+\.(py|ts|tsx|sql)\b/i },
  { name: "a module path", pattern: /\b(packages|services|src)\/[a-z]/i },
  // A column or field name. User-facing English does not contain snake_case.
  { name: "a database or payload field", pattern: /\b[a-z][a-z0-9]*(_[a-z0-9]+)+\b/ },
  // An environment variable or an alert name, which is the same defect wearing capitals.
  { name: "a setting or alert name", pattern: /\b[A-Z][A-Z0-9]*(_[A-Z0-9]+)+\b/ },
];

/** What a cell may never contain: a dash, a blank, or a shrug. */
const BARE = new Set(["—", "–", "-", "", "N/A", "n/a", "NA", "null", "undefined", "NaN"]);

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

/**
 * Every cell that holds a figure says something, and what it says is never a dash.
 *
 * Table cells and definition values are checked rather than the whole page, because those are the
 * places a figure goes — a paragraph containing an em dash is punctuation, a cell containing one
 * is a missing number pretending to be a rendered one.
 */
function noBareCells(label: string): void {
  const cells = document.querySelectorAll("td, dd");
  expect(cells.length, `${label}: nothing was rendered to check`).toBeGreaterThan(0);
  for (const cell of cells) {
    const text = (cell.textContent ?? "").trim();
    expect(
      BARE.has(text),
      `${label}: a figure rendered as "${text}" with no reason beside it`,
    ).toBe(false);
  }
}

describe("nothing internal reaches the twt reader", () => {
  it("copy: the hub with a full session names no route, no column and no setting", () => {
    const view = todayView(today());
    render(
      <>
        <GateCard gate={gate()} />
        <TightNames rows={view.tight} session="2026-09-10" />
        <OpenPositions rows={view.positions} />
        <HalfSizeCounter halfSize={halfSize()} />
      </>,
    );

    expect(screen.getByTestId("twt-gate-badge")).toBeInTheDocument();
    scan("the hub with a session");
  });

  /* The empty state is where the defect lived last time: nothing to show is the case where a page
     starts explaining itself, and an explanation written for the author names the author's things. */
  it("copy: the hub with nothing computed at all stays in a reader's words", () => {
    const view = todayView(emptyToday());
    render(
      <>
        <GateCard gate={null} />
        <TightNames rows={view.tight} session={null} />
        <OpenPositions rows={view.positions} />
        <HalfSizeCounter halfSize={halfSize()} />
      </>,
    );

    scan("the empty hub");
  });

  it("copy: an unprotected position explains itself without naming the machinery", () => {
    const view = todayView(today({ positions: [nakedPosition()] }));
    render(<OpenPositions rows={view.positions} />);

    expect(screen.getByTestId("twt-naked")).toBeInTheDocument();
    scan("an unprotected position");
  });

  it("copy: the backtest page says what a reader needs and nothing about the runner", () => {
    render(<BacktestCard backtest={backtest()} />);
    scan("the backtest page");
  });

  it("copy: the backtest page with no completed run names no job and no table", () => {
    render(<BacktestCard backtest={null} />);
    scan("the backtest page with no run");
  });

  /* The desk page is read by an operator rather than by a customer, and the rule still holds: the
     mode badge says DRY RUN, not the name of the variable that sets it, and a skip says why in
     words rather than in the tag it is stored under. */
  it("copy: the desk page shows the mode and the skips without their stored spellings", () => {
    render(<DeskPlan view={deskView(deskPlan(), new Date("2026-09-10T18:10:00+05:30"))} />);

    expect(screen.getByTestId("twt-desk-mode")).toHaveTextContent("DRY RUN");
    scan("the desk page");
  });
});

describe("no figure on twt is ever unavailable without its reason", () => {
  it("unavailable: a position with no live price says so instead of showing a dash", () => {
    const view = todayView(
      today({
        positions: [
          nakedPosition({ last_price: null, unrealised_inr: null, unrealised_fraction: null }),
        ],
      }),
    );
    render(<OpenPositions rows={view.positions} />);

    const reasons = screen.getAllByTestId("twt-unavailable");
    expect(reasons.length).toBeGreaterThan(0);
    for (const reason of reasons) {
      expect((reason.textContent ?? "").length).toBeGreaterThan(8);
    }
    noBareCells("a position with no live price");
  });

  it("unavailable: an empty hub renders no bare cell anywhere", () => {
    const view = todayView(emptyToday());
    render(
      <>
        <TightNames rows={view.tight} session={null} />
        <OpenPositions rows={view.positions} />
      </>,
    );
    /* Nothing to put in a cell is not nothing to say: the empty states are sentences, and the
       assertion is that no half-rendered row survived beside them. */
    expect(screen.getByTestId("twt-open-empty")).toBeInTheDocument();
  });

  it("unavailable: a run with no statistics shows its reason, not an empty column", () => {
    render(
      <BacktestCard
        backtest={backtest([
          { ...backtest().runs[0]!, id: 3, finished_at: "2026-09-10T21:00:00+05:30" },
        ])}
      />,
    );

    expect(screen.getByTestId("twt-run-unavailable")).toHaveTextContent(/No completed run yet/i);
    noBareCells("a run with no statistics");
  });

  it("unavailable: a desk line missing a price says which figure it cannot give", () => {
    render(
      <DeskPlan
        view={deskView(
          deskPlan({
            lines: [
              {
                ...deskPlan().lines[0]!,
                last_price: null,
                value_inr: null,
                rank_key: null,
              },
            ],
          }),
          new Date("2026-09-10T18:10:00+05:30"),
        )}
      />,
    );

    expect(screen.getAllByTestId("twt-unavailable").length).toBeGreaterThan(0);
    noBareCells("a desk line missing a price");
  });
});
