import type { PipelineRunDetailOut, ProviderHealthOut } from "@baskfy/api-client";
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { DataVersions } from "@/components/admin/data-versions";
import { PipelineRuns } from "@/components/admin/pipeline-runs";
import { ProviderHealth } from "@/components/admin/provider-health";
import { UserMenu } from "@/components/shell/user-menu";
import { formatDateTimeIST } from "@/lib/format";

/**
 * The staff surface — PROMPTS.md Prompt 17 deliverable 4, docs/09 §Observability's
 * "`pipeline_run_step` is the operator UI".
 *
 * The assertions are about what an operator needs at 3am, not about layout: that a failed step is
 * named rather than merely coloured (docs/11 §Accessibility: "colour is never the sole carrier of
 * meaning"), that the step payload survives to the page verbatim, that `data_version` has no
 * roll-back button, and that the admin link is rendered from server truth rather than guessed.
 */

// The server actions the buttons call. Mocked because a component test must not reach the API,
// and because the action itself is asserted server-side.
vi.mock("@/app/actions/admin", () => ({
  rerunPipeline: vi.fn(() => Promise.resolve({ ok: true, message: "queued" })),
  reprocessInstrument: vi.fn(() => Promise.resolve({ ok: true, message: "queued" })),
  setEntitlementOverride: vi.fn(() => Promise.resolve({ ok: true, message: "granted" })),
  clearEntitlementOverride: vi.fn(() => Promise.resolve({ ok: true, message: "removed" })),
}));

const FAILED_RUN: PipelineRunDetailOut = {
  id: 41,
  trade_date: "2026-08-18",
  status: "failed",
  started_at: "2026-08-18T13:15:00Z",
  finished_at: "2026-08-18T13:40:00Z",
  data_version: null,
  publish_latency_seconds: null,
  steps: [
    {
      step: "compute_factors",
      status: "succeeded",
      rows_in: 2300,
      rows_out: 2300,
      duration_ms: 12_500,
      detail: { engine: "polars" },
    },
    {
      step: "data_quality_gate",
      status: "failed",
      rows_in: 8,
      rows_out: 8,
      duration_ms: 900,
      detail: { failures: [{ assertion: 1, message: "only 12% of instruments have a bar" }] },
    },
  ],
};

describe("the pipeline run history", () => {
  it("names the failed step rather than only colouring it", () => {
    render(<PipelineRuns runs={[FAILED_RUN]} />);
    // docs/11 §Accessibility. A greyscale screenshot must still say which step broke.
    expect(screen.getByText(/1 failed \(data_quality_gate\)/)).toBeInTheDocument();
  });

  it("shows every step with its row counts and duration", () => {
    render(<PipelineRuns runs={[FAILED_RUN]} />);
    expect(screen.getByText("compute_factors")).toBeInTheDocument();
    expect(screen.getByText("12.5 s")).toBeInTheDocument();
    expect(screen.getAllByText("2300").length).toBe(2);
  });

  it("renders the step payload verbatim, not a summary of it", () => {
    // At 3am the useful thing is the failing assertion list as the gate wrote it.
    render(<PipelineRuns runs={[FAILED_RUN]} />);
    const payloads = screen.getByText(/Step payloads/);
    expect(payloads).toBeInTheDocument();
    expect(screen.getByText(/only 12% of instruments have a bar/)).toBeInTheDocument();
  });

  it("offers a re-run for every date", () => {
    render(<PipelineRuns runs={[FAILED_RUN]} />);
    expect(screen.getByRole("button", { name: /re-run/i })).toBeInTheDocument();
  });

  it("says so rather than rendering an empty table when nothing has run", () => {
    render(<PipelineRuns runs={[]} />);
    expect(screen.getByText(/No pipeline runs yet/i)).toBeInTheDocument();
  });

  it("shows an em dash for a run that never published", () => {
    render(<PipelineRuns runs={[FAILED_RUN]} />);
    const row = screen.getByText("2026-08-18").closest("tr");
    expect(row).not.toBeNull();
    expect(within(row as HTMLElement).getAllByText("—").length).toBeGreaterThanOrEqual(2);
  });
});

describe("the data_version history", () => {
  const rows = [
    {
      data_version: 42,
      trade_date: "2026-08-18",
      published_at: "2026-08-18T14:40:00Z",
      publish_latency_seconds: 16_200,
    },
    {
      data_version: 41,
      trade_date: "2026-08-17",
      published_at: "2026-08-17T14:20:00Z",
      publish_latency_seconds: 15_000,
    },
  ];

  it("marks the current version", () => {
    render(<DataVersions rows={rows} current={42} />);
    expect(screen.getByText("current")).toBeInTheDocument();
  });

  it("has no roll-back control", () => {
    // `data_version` is the gate's output (docs/03 step 10). A UI that can move it backwards is a
    // UI that can move it forwards over data the gate rejected; the procedure is a runbook.
    render(<DataVersions rows={rows} current={42} />);
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("explains an empty history instead of showing nothing", () => {
    render(<DataVersions rows={[]} current={0} />);
    expect(screen.getByText(/pipeline-degraded/)).toBeInTheDocument();
  });
});

describe("provider health", () => {
  const providers: ProviderHealthOut[] = [
    {
      name: "kite",
      available: false,
      detail: "no access token is stored",
      serves_now: [],
      can_serve: ["bars"],
    },
    { name: "nse", available: true, detail: "", serves_now: ["indices"], can_serve: ["indices"] },
  ];

  it("distinguishes what an adapter can serve from what it is serving", () => {
    // The gap between the two columns is the diagnosis: `can_serve` without `serves_now` is a
    // credential or a token problem, not a capability one.
    render(<ProviderHealth providers={providers} />);
    const kite = screen.getByText("kite").closest("tr") as HTMLElement;
    expect(within(kite).getByText("down")).toBeInTheDocument();
    expect(within(kite).getByText("no access token is stored")).toBeInTheDocument();
    expect(within(kite).getByText("bars")).toBeInTheDocument();
  });

  it("uses a word, not only a colour, for availability", () => {
    render(<ProviderHealth providers={providers} />);
    expect(screen.getByText("ok")).toBeInTheDocument();
    expect(screen.getByText("down")).toBeInTheDocument();
  });
});

describe("the admin link in the user menu", () => {
  it("is absent for a signed-in account that is not staff", () => {
    render(<UserMenu email="them@example.com" name="Them" isStaff={false} />);
    expect(screen.queryByText("Admin")).toBeNull();
  });

  it("defaults to absent when the flag is not supplied at all", () => {
    render(<UserMenu email="them@example.com" name="Them" />);
    expect(screen.queryByText("Admin")).toBeNull();
  });
});

describe("operator timestamps", () => {
  it("renders in IST and says which zone it is", () => {
    // docs/09's schedule and docs/11's 20:15 SLO are IST wall-clock times tied to the NSE
    // session. Rendering a publish time in the reader's zone makes "was this late?" arithmetic.
    expect(formatDateTimeIST("2026-08-18T14:40:00Z")).toBe("18 Aug 2026, 20:10 IST");
  });

  it("returns the em dash for a missing timestamp", () => {
    expect(formatDateTimeIST(null)).toBe("—");
    expect(formatDateTimeIST("not a date")).toBe("—");
  });
});
