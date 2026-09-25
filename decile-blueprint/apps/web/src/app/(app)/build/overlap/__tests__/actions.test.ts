import { beforeEach, describe, expect, it, vi } from "vitest";

const revalidatePath = vi.fn<(path: string) => void>();
vi.mock("next/cache", () => ({ revalidatePath: (path: string) => revalidatePath(path) }));

const overlapWrite = vi.fn<(...args: unknown[]) => Promise<unknown>>();
vi.mock("@/lib/overlap/write", () => ({
  overlapWrite: (...args: unknown[]): Promise<unknown> => overlapWrite(...args),
}));

const { filingsScanStatus, startFilingsScan, correctTag } = await import("../actions");

const done = {
  state: "done",
  scope: "actionable",
  total: 15,
  done: 15,
  announcements: 6400,
  earnings_dates: 0,
  rows_written: 5853,
  failed: [],
  error: null,
  queued_at: null,
  started_at: "2026-09-25T00:02:15+00:00",
  finished_at: "2026-09-25T00:02:42+00:00",
};

beforeEach(() => {
  revalidatePath.mockClear();
  overlapWrite.mockReset();
});

describe("the overlap page's server actions", () => {
  it("reading the scan's status never re-renders the page, even when the last scan is done", async () => {
    /* Every scan ends in `done` and stays there, so a status read that revalidated on `done`
       re-rendered the page on every mount — and the second render's candidates read lost the
       race against the screen re-runs, so the table blinked out over the old matrix (staging,
       25 Sep 2026). The running→done edge the button observes is what refreshes. */
    overlapWrite.mockResolvedValue({ ok: true, status: 200, body: done });
    const result = await filingsScanStatus();
    expect(result).toEqual({ ok: true, scan: done });
    expect(revalidatePath).not.toHaveBeenCalled();
  });

  it("starting a scan is a write and reads back the queued state without a re-render", async () => {
    overlapWrite.mockResolvedValue({ ok: true, status: 202, body: { ...done, state: "queued" } });
    const result = await startFilingsScan("actionable");
    expect(result.ok).toBe(true);
    expect(revalidatePath).not.toHaveBeenCalled();
  });

  it("a correction is the write that re-renders the page", async () => {
    overlapWrite.mockResolvedValue({
      ok: true,
      status: 200,
      body: {
        event_type: "order",
        review_priority: "high",
        matched: [],
        source: "corrected",
        confidence: null,
        disagrees_with: null,
        corrected: true,
      },
    });
    const result = await correctTag("Receipt of order", "order");
    expect(result.ok).toBe(true);
    expect(revalidatePath).toHaveBeenCalledWith("/build/overlap");
  });
});
