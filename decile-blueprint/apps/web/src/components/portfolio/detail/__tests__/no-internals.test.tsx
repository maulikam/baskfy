import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AllocationTab } from "@/components/portfolio/detail/allocation-tab";
import { HoldingsTab } from "@/components/portfolio/detail/holdings-tab";
import { RiskTab } from "@/components/portfolio/detail/risk-tab";
import {
  BLOCKED_ALLOCATION,
  BLOCKED_COLUMNS,
  BLOCKED_OVERVIEW,
  BLOCKED_PERFORMANCE,
  BLOCKED_RISK,
  allocationView,
  holdingRows,
  holdingsContext,
  riskView,
} from "@/lib/portfolio/detail-tabs";

import { barrenDetail, emptyDetail, richDetail, richNav } from "./fixtures";
import { renderWorkspace } from "./render";

/**
 * Nothing internal reaches the reader — the **detail tabs**, which the existing scan could not see.
 *
 * `components/portfolio/__tests__/no-internals.test.tsx` was written on 11 Sep 2026 after a browser
 * screenshot found `GET /api/v1/portfolio/{id}/nav`, `net_flow`, `packages/core` and
 * `todays_contribution` on a customer-facing screen. It renders `CommandCenterScreen` and only
 * that. Every one of the four `BLOCKED_*` lists behind the Holdings, Allocation, Risk and
 * Performance tabs was outside its reach, and on 12 Sep 2026 five internals were still sitting in
 * them, rendered verbatim by `primitives.tsx`'s `BlockedList` as `Not shown because {why}.` and
 * `It needs: {unblockedBy}.`:
 *
 * | where | what a retail investor was reading |
 * |---|---|
 * | `BLOCKED_COLUMNS` "Exchange and instrument type" | `InstrumentRefOut` |
 * | `BLOCKED_COLUMNS` "Available quantity…" | `t1_quantity`, `collateral_quantity`, `DetailHoldingOut` |
 * | `BLOCKED_COLUMNS` "Brokerage, taxes and charges" | `packages/core` |
 * | `BLOCKED_RISK` "Beta against the benchmark" | `portfolio_nav_daily` |
 *
 * The last of those was **pinned by a passing test** — `risk.test.tsx` asserted
 * `"a statistics job over portfolio_nav_daily"` as the correct sentence — which is the failure
 * mode this file is the answer to. A green test asserting the wrong thing is worse than no test:
 * it looks like coverage and it defends the defect.
 *
 * The engineering detail is not deleted. It moved to a code comment beside each entry, where the
 * person who can act on it is the one reading.
 *
 * Two scans, because they fail at different times. The **data** scan reads the lists directly and
 * catches a bad string the moment it is authored, including in the two tabs that have no cheap
 * render. The **render** scan reads the page as a person does and catches a leak that arrives
 * from anywhere else — a fixture, a reason constant, an interpolated field name.
 */

const BANNED: ReadonlyArray<{ readonly name: string; readonly pattern: RegExp }> = [
  { name: "an API route", pattern: /\b(GET|POST|PATCH|PUT|DELETE)\s+\//i },
  { name: "an API path", pattern: /\/api\/v\d/i },
  { name: "a host and port", pattern: /https?:\/\/[^\s)]+/i },
  { name: "a source file", pattern: /\b[\w/-]+\.(py|ts|tsx|sql)\b/i },
  { name: "a module path", pattern: /\b(packages|services|src)\/[a-z]/i },
  // A column or field name. User-facing English does not contain snake_case.
  { name: "a database or payload field", pattern: /\b[a-z][a-z0-9]*(_[a-z0-9]+)+\b/ },
  // An environment variable or an alert name: the same defect wearing capitals.
  { name: "a setting or alert name", pattern: /\b[A-Z][A-Z0-9]*(_[A-Z0-9]+)+\b/ },
  // A generated schema type. `DetailHoldingOut` is not a thing a person owns.
  { name: "a payload type", pattern: /\b[A-Z][A-Za-z]*(Out|In|Patch|Ref)\b/ },
];

function check(label: string, text: string): void {
  for (const { name, pattern } of BANNED) {
    const hit = pattern.exec(text);
    expect(
      hit,
      `${label}: ${name} reached the reader — "${hit?.[0] ?? ""}". Say what a reader needs; put the identifier in the code comment.`,
    ).toBeNull();
  }
}

function scanPage(label: string): void {
  const text = document.body.textContent ?? "";
  expect(text.length, `${label}: nothing was rendered to scan`).toBeGreaterThan(0);
  check(label, text);
}

describe("no blocked-figure explanation is written in the system's own vocabulary", () => {
  const lists = [
    ["the Holdings tab's blocked columns", BLOCKED_COLUMNS],
    ["the Allocation tab's blocked cuts", BLOCKED_ALLOCATION],
    ["the Risk tab's unmeasured figures", BLOCKED_RISK],
    ["the Performance tab's blocked figures", BLOCKED_PERFORMANCE],
    ["the Overview tab's blocked figures", BLOCKED_OVERVIEW],
  ] as const;

  for (const [label, list] of lists) {
    it(`copy: ${label} name no table, no column, no module and no payload type`, () => {
      expect(list.length, `${label}: the list is empty, so this asserts nothing`).toBeGreaterThan(
        0,
      );
      for (const item of list) {
        check(`${label} — "${item.name}" (name)`, item.name);
        check(`${label} — "${item.name}" (why)`, item.why);
        check(`${label} — "${item.name}" (it needs)`, item.unblockedBy);
      }
    });
  }
});

describe("nothing internal reaches the portfolio detail reader", () => {
  it("copy: the Risk tab with a full history stays in a reader's words", () => {
    renderWorkspace(<RiskTab risk={riskView(richDetail(), richNav())} />);
    expect(screen.queryByTestId("risk-blocked")).not.toBeInTheDocument();
    scanPage("the Risk tab with a history");
  });

  /* The empty state is where the defect lives: nothing to show is the case where a page starts
     explaining itself, and an explanation written for the author names the author's things. */
  it("copy: the Risk tab with nothing measured at all explains itself in a reader's words", () => {
    renderWorkspace(<RiskTab risk={riskView(barrenDetail(), null)} />);
    scanPage("the Risk tab with nothing measured");
  });

  it("copy: the Allocation tab names no column and no instrument table", () => {
    renderWorkspace(<AllocationTab allocation={allocationView(richDetail())} />);
    expect(screen.queryByTestId("allocation-blocked")).not.toBeInTheDocument();
    scanPage("the Allocation tab");
  });

  it("copy: the Allocation tab with nothing priced says so without naming the payload", () => {
    renderWorkspace(<AllocationTab allocation={allocationView(emptyDetail())} />);
    scanPage("the Allocation tab with nothing priced");
  });

  it("copy: the Holdings tab names no broker column and no cost model's module", () => {
    const detail = richDetail();
    renderWorkspace(
      <HoldingsTab rows={holdingRows(detail)} basketBacked={holdingsContext(detail).basketBacked} />,
    );
    scanPage("the Holdings tab");
  });

  it("copy: a Holdings read that failed says so without the machinery that failed", () => {
    renderWorkspace(
      <HoldingsTab
        rows={[]}
        basketBacked={false}
        unavailableReason="The holdings for this portfolio did not load."
      />,
    );
    scanPage("the Holdings tab with a failed read");
  });
});
