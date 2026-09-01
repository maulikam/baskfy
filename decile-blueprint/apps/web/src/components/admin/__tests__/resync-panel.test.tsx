import type { ResyncPlanOut } from "@baskfy/api-client";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ResyncPanel } from "@/components/admin/resync-panel";

/**
 * Leaf 3.1's button, from the operator's side.
 *
 * The assertions are about honesty, not layout. Three states have to be distinguishable at a
 * glance on a phone — nothing pending, work found, and a repair that only half worked — and the
 * fourth state the panel must never have is "nothing pending" rendered over a check that never
 * ran.
 */

vi.mock("@/app/actions/admin", () => ({
  inspectResync: vi.fn(() => Promise.resolve({ ok: true, plan: null, message: "" })),
  startResync: vi.fn(() => Promise.resolve({ ok: true, message: "queued" })),
}));

function plan(overrides: Partial<ResyncPlanOut> = {}): ResyncPlanOut {
  return {
    window_start: "2025-07-25",
    window_end: "2026-08-28",
    trading_days_checked: 261,
    pending: false,
    findings: [],
    unresolved: [],
    last_resync: null,
    ...overrides,
  };
}

describe("nothing pending", () => {
  it("says so, and says what it checked", () => {
    // The common answer, and a real one. A panel that renders empty here is indistinguishable
    // from a panel whose check failed.
    render(<ResyncPanel initial={plan()} />);
    expect(screen.getByText(/Nothing is pending/)).toBeInTheDocument();
    expect(screen.getByText(/261 trading days checked/)).toBeInTheDocument();
  });

  it("does not claim an all-clear when part of the check could not run", () => {
    // "I could not look" must never render as "I looked and it was fine" — that conflation is
    // the exact bug the 2026-08-28 detector exists to catch.
    render(
      <ResyncPanel
        initial={plan({
          unresolved: ["No provider on this host can ask NSE whether it published for 2026-08-28."],
        })}
      />,
    );
    expect(screen.queryByText(/Nothing is pending/)).not.toBeInTheDocument();
    expect(screen.getByText(/Nothing pending in what could be checked/)).toBeInTheDocument();
    expect(screen.getByText(/can ask NSE/)).toBeInTheDocument();
  });
});

describe("work pending", () => {
  const pending = plan({
    pending: true,
    findings: [
      {
        kind: "thin_bars",
        trade_date: "2026-02-01",
        summary: "2026-02-01 holds 322 bars against a neighbouring median of 2,310.",
        remedy: "re-ingest the NSE bhavcopy for the date",
        observed_bars: 322,
        expected_bars: 2310,
      },
      {
        kind: "kite_session",
        trade_date: null,
        summary: "The stored Kite session was issued on 2026-08-27 and has expired.",
        remedy: "python -m baskfy_worker.kite_session_cli pull",
        observed_bars: null,
        expected_bars: null,
      },
    ],
  });

  it("names each gap with the number behind it", () => {
    render(<ResyncPanel initial={pending} />);
    expect(screen.getByText(/322 bars against a neighbouring median/)).toBeInTheDocument();
    expect(screen.getByText("322 of ~2,310 bars")).toBeInTheDocument();
  });

  it("says what pressing the button will actually run, before it runs", () => {
    render(<ResyncPanel initial={pending} />);
    expect(screen.getByText(/re-ingest the NSE bhavcopy for the date/)).toBeInTheDocument();
  });

  it("counts the pending items on the button itself", () => {
    render(<ResyncPanel initial={pending} />);
    expect(screen.getByRole("button", { name: /Resync 2 pending items/ })).toBeInTheDocument();
  });

  it("renders a finding that is about the host rather than a date", () => {
    render(<ResyncPanel initial={pending} />);
    expect(screen.getByText("this host")).toBeInTheDocument();
    expect(screen.getByText("broker session")).toBeInTheDocument();
  });
});

describe("a repair that only half worked", () => {
  const partial = plan({
    last_resync: {
      completed_at: "2026-08-28T15:40:00Z",
      actor: "ops@example.com",
      repaired: ["2026-02-01: re-ingested 2,304 bars from the NSE bhavcopy"],
      failed: ["2026-01-30: NSE published no bhavcopy for this date"],
      deferred: ["2025-11-04: not re-ingested this pass (30 days per press)"],
      still_pending: ["2026-01-30 holds 12 bars against a neighbouring median of 2,290"],
      unresolved: [],
      complete: false,
    },
  });

  it("refuses to report success", () => {
    // G7: a resync that fixes three of five gaps and reports success has lied.
    render(<ResyncPanel initial={partial} />);
    expect(screen.getByText(/Did NOT close everything/)).toBeInTheDocument();
  });

  it("shows what it could not fix and what it never got to, separately", () => {
    render(<ResyncPanel initial={partial} />);
    expect(screen.getByText(/Could not fix · 1/)).toBeInTheDocument();
    expect(screen.getByText(/Not attempted yet · 1/)).toBeInTheDocument();
    expect(screen.getByText(/Still pending · 1/)).toBeInTheDocument();
    expect(screen.getByText(/NSE published no bhavcopy/)).toBeInTheDocument();
  });

  it("still credits what it did fix", () => {
    render(<ResyncPanel initial={partial} />);
    expect(screen.getByText(/re-ingested 2,304 bars/)).toBeInTheDocument();
  });
});

describe("a repair that worked", () => {
  it("says so plainly", () => {
    render(
      <ResyncPanel
        initial={plan({
          last_resync: {
            completed_at: "2026-08-28T15:40:00Z",
            actor: "ops@example.com",
            repaired: ["Kite session: pulled a fresh access token from the desk"],
            failed: [],
            deferred: [],
            still_pending: [],
            unresolved: [],
            complete: true,
          },
        })}
      />,
    );
    expect(screen.getByText(/Closed everything it found/)).toBeInTheDocument();
  });
});

describe("before any check has run", () => {
  it("says the check has not run rather than rendering an all-clear", () => {
    render(<ResyncPanel initial={null} />);
    expect(screen.getByText(/The data check has not run yet/)).toBeInTheDocument();
    expect(screen.queryByText(/Nothing is pending/)).not.toBeInTheDocument();
  });
});
