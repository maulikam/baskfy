import { beforeEach, describe, expect, it, vi } from "vitest";

const revalidatePath = vi.fn<(path: string) => void>();
vi.mock("next/cache", () => ({ revalidatePath: (path: string) => revalidatePath(path) }));

const overlapWrite = vi.fn<(...args: unknown[]) => Promise<unknown>>();
vi.mock("@/lib/overlap/write", () => ({
  overlapWrite: (...args: unknown[]): Promise<unknown> => overlapWrite(...args),
}));

const { filingsScanStatus, startFilingsScan, correctTag, labelRow } = await import("../actions");

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

  it("a row label is a PUT on /overlap/reviews with the instrument and the word, and re-renders", async () => {
    overlapWrite.mockResolvedValue({
      ok: true,
      status: 200,
      body: {
        label: "skip",
        confidence: 0,
        source: "labelled",
        shown: true,
        floor: 0.6,
        labelled: true,
      },
    });
    const result = await labelRow(42, "skip");
    expect(result).toEqual({ ok: true });
    expect(overlapWrite).toHaveBeenCalledWith("/overlap/reviews", {
      method: "PUT",
      body: { instrument_id: 42, label: "skip", note: null },
    });
    expect(revalidatePath).toHaveBeenCalledWith("/build/overlap");
  });

  it("clearing a row label is a DELETE, and an already-absent label still re-renders", async () => {
    overlapWrite.mockResolvedValue({ ok: false, status: 404 });
    const result = await labelRow(42, null);
    expect(result).toEqual({ ok: true });
    expect(overlapWrite).toHaveBeenCalledWith("/overlap/reviews", {
      method: "DELETE",
      query: { instrument_id: 42 },
    });
    expect(revalidatePath).toHaveBeenCalledWith("/build/overlap");
  });

  it("a refused row label is said in this app's words and nothing is re-rendered", async () => {
    overlapWrite.mockResolvedValue({ ok: false, status: 404 });
    const result = await labelRow(42, "look_first");
    expect(result).toEqual({
      ok: false,
      error: "That name is not on today's list, or labels belong to the account that runs the scans.",
    });
    expect(revalidatePath).not.toHaveBeenCalled();
    /* A word outside the vocabulary never reaches the wire. */
    overlapWrite.mockClear();
    const bad = await labelRow(42, "buy" as unknown as "skip");
    expect(bad.ok).toBe(false);
    expect(overlapWrite).not.toHaveBeenCalled();
  });
});
