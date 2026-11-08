import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { FilingsScanButton, scanLine } from "@/components/overlap/filings-scan-button";
import type { FilingsScan, FilingsScanActions } from "@/lib/overlap/candidates";

const refresh = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh }) }));

afterEach(() => {
  cleanup();
  refresh.mockReset();
  vi.useRealTimers();
});

function scan(overrides: Partial<FilingsScan>): FilingsScan {
  return { state: "idle", done: 0, failed: [], ...overrides };
}

describe("scanLine", () => {
  it("says nothing before the first scan", () => {
    expect(scanLine(null)).toBeNull();
    expect(scanLine(scan({ state: "idle" }))).toBeNull();
  });

  it("counts names while running", () => {
    expect(scanLine(scan({ state: "running", total: 34, done: 12 }))).toBe(
      "Reading filings: 12 of 34 names…",
    );
  });

  it("names the failures and that Laya tags next when done", () => {
    expect(scanLine(scan({ state: "done", total: 34, done: 34, failed: ["AAA"] }))).toBe(
      "Filings read for 34 names (1 could not be read) — Laya tags the new headlines within a minute.",
    );
  });
});

describe("FilingsScanButton", () => {
  it("starts a scan for the scope in view, shows progress, and refreshes the page when done", async () => {
    vi.useFakeTimers();
    const statuses = [
      scan({ state: "idle" }),
      scan({ state: "running", total: 2, done: 1 }),
      scan({ state: "done", total: 2, done: 2 }),
    ];
    const actions: FilingsScanActions = {
      start: vi.fn(() => Promise.resolve({ ok: true as const, scan: scan({ state: "queued" }) })),
      status: vi.fn(() => Promise.resolve({ ok: true as const, scan: statuses.shift() ?? scan({ state: "done", total: 2, done: 2 }) })),
    };
    const tick = async (ms: number) => {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(ms);
      });
    };
    render(<FilingsScanButton scope="all" actions={actions} />);
    await tick(0);
    expect(actions.status).toHaveBeenCalledTimes(1);

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Scan filings with Laya" }));
      await Promise.resolve();
    });
    await tick(0);
    expect(actions.start).toHaveBeenCalledWith("all");
    expect(screen.getByText("Queued — starting shortly.")).toBeTruthy();
    expect(screen.getByRole("button").hasAttribute("disabled")).toBe(true);

    await tick(4000);
    expect(screen.getByText("Reading filings: 1 of 2 names…")).toBeTruthy();

    await tick(4000);
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Scan filings with Laya" })).toBeTruthy();
  });

  it("shows the refusal in the app's words and starts nothing", async () => {
    const actions: FilingsScanActions = {
      start: vi.fn(() => Promise.resolve({ ok: false as const, error: "A filings scan is already running." })),
      status: vi.fn(() => Promise.resolve({ ok: true as const, scan: scan({ state: "idle" }) })),
    };
    render(<FilingsScanButton scope="actionable" actions={actions} />);
    fireEvent.click(screen.getByRole("button", { name: "Scan filings with Laya" }));
    expect(await screen.findByText("A filings scan is already running.")).toBeTruthy();
    expect(refresh).not.toHaveBeenCalled();
  });
});
