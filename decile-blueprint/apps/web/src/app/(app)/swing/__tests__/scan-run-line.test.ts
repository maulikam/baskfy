import { describe, expect, it } from "vitest";

import type { SwingScanRun } from "@/lib/swing/fetch";

import { neverScannedLine, scanRunLine } from "../copy";

/**
 * What the book says about its own last scan — the gap of 12 Sep 2026, closed.
 *
 * "None of the scan shows when the last scan performed in any strategy." The plumbing was here:
 * `/swing/setups` has served `last_scan` since SW15 and this function has existed just as long.
 * What it said was the empty string until somebody pressed the button, and a bare stamp
 * afterwards. A reader wants three facts — **when it ran, whether it finished, and whether it
 * found anything** — and each state below is one of the ways those three can come out.
 *
 * The sibling sleeves' equivalents are `app/(app)/{vbt,twt}/__tests__/scan-now.test.tsx`, which
 * assert the same five states through the rendered control. This file asserts the sentences
 * themselves, because the book's carry one thing the others' do not: whether the rows were built
 * from live quotes or from published bars (SW15's `provisional`).
 */

const FINISHED = "2026-09-12T11:29:00+00:00"; // 16:59 IST
const finished = new Date(FINISHED).getTime();

function run(overrides: Partial<SwingScanRun> = {}): SwingScanRun {
  return {
    run_id: 7,
    status: "DONE",
    requested_at: "2026-09-12T11:28:41+00:00",
    started_at: "2026-09-12T11:28:42+00:00",
    finished_at: FINISHED,
    session_date: "2026-09-11",
    provisional: false,
    funnel: { instruments: 1812, liquid: 41, candidates: { FLAG: 2, EP: 0 } },
    detail: null,
    error: null,
    ...overrides,
  };
}

describe("when it ran", () => {
  it("says the age while the run is recent", () => {
    expect(scanRunLine(run(), { now: finished + 12 * 60_000 })).toBe(
      "Last scanned 12 minutes ago from published bars — 41 liquid, 2 setups.",
    );
  });

  it("says the IST wall clock once it is not, and on the server's pass", () => {
    const stamped = "Last scanned at 12 Sept 2026, 16:59 IST from published bars — 41 liquid, 2 setups.";
    expect(scanRunLine(run(), { now: finished + 3 * 24 * 3_600_000 })).toBe(stamped);
    expect(scanRunLine(run(), { now: null })).toBe(stamped);
  });

  it("says which bar it read, because a live-quote scan is provisional", () => {
    expect(scanRunLine(run({ provisional: true }), { now: finished + 60_000 })).toContain(
      "from live quotes",
    );
  });
});

describe("whether it found anything", () => {
  it("counts the flags across every kind of setup", () => {
    expect(
      scanRunLine(run({ funnel: { liquid: 41, candidates: { FLAG: 2, EP: 3 } } }), {
        now: finished + 60_000,
      }),
    ).toContain("41 liquid, 5 setups");
  });

  it("says one setup rather than 1 setups", () => {
    expect(
      scanRunLine(run({ funnel: { liquid: 41, candidates: { FLAG: 1 } } }), {
        now: finished + 60_000,
      }),
    ).toContain("41 liquid, 1 setup.");
  });

  /**
   * Zero is the ordinary answer, not a fault — `docs/swing/05` §2 makes the funnel compulsory for
   * exactly this reason. It must also be distinguishable from a run that never said, which is why
   * the two branches below produce different sentences rather than the same one with a nought.
   */
  it("says a run that flagged nothing found nothing, without reading as a failure", () => {
    const line = scanRunLine(run({ funnel: { liquid: 41, candidates: {} } }), {
      now: finished + 60_000,
    });
    expect(line).toBe("Last scanned a minute ago from published bars — 41 liquid, no setups, which is an ordinary day.");
    expect(line).not.toMatch(/fail|error|wrong|problem/i);
  });

  it("says only when it ran when the run carried no funnel at all", () => {
    expect(scanRunLine(run({ funnel: null }), { now: finished + 60_000 })).toBe(
      "Last scanned a minute ago from published bars.",
    );
  });
});

describe("whether it finished", () => {
  it("says a scan is queued, and that the page will keep up with it", () => {
    expect(scanRunLine(run({ status: "QUEUED" }), {})).toBe(
      "Scan queued. This page updates when it finishes.",
    );
  });

  it("says a scan is running", () => {
    expect(scanRunLine(run({ status: "RUNNING" }), {})).toBe(
      "Scanning now. This page updates when it finishes.",
    );
  });

  /**
   * THE REASON IS NOT THE READER'S. `run.error` names jobs, quote sources and tables; it is
   * written for whoever can fix it. Until 12 Sep 2026 this line repeated it verbatim onto a
   * customer-facing page — the defect of 11 Sep 2026 said again, and pinned by a test.
   */
  it("says a run did not finish, and when, and never why", () => {
    const line = scanRunLine(
      run({
        status: "FAILED",
        error: "ScanNotRunnable: no quote source while ohlcv_daily is mid-write",
      }),
      { now: finished + 12 * 60_000 },
    );
    expect(line).toBe(
      "The last scan did not finish, so nothing changed. It stopped 12 minutes ago. " +
        "You can start another.",
    );
    expect(line).not.toContain("ScanNotRunnable");
    expect(line).not.toContain("ohlcv_daily");
  });
});

describe("never scanned at all", () => {
  /**
   * Two different states and two different sentences. A book with a published session HAS been
   * scanned — by the nightly, which is what wrote the session — and saying nothing there is what
   * made three strategies look as though they had never run.
   */
  it("names the nightly run when a session is published but nobody has pressed the button", () => {
    expect(scanRunLine(null, { session: "2026-09-11" })).toBe(
      "No scan has been started from here yet — what is shown is the nightly run's, for the " +
        "11 Sept 2026 session.",
    );
    expect(neverScannedLine("2026-09-11")).toBe(scanRunLine(null, { session: "2026-09-11" }));
  });

  it("says no scan has run when nothing has been published either", () => {
    expect(scanRunLine(null, {})).toBe("No scan has run yet.");
    expect(scanRunLine(undefined, { session: null })).toBe("No scan has run yet.");
  });
});

describe("nothing from the inside of the system reaches the reader", () => {
  const BANNED: ReadonlyArray<{ readonly name: string; readonly pattern: RegExp }> = [
    { name: "an API route", pattern: /\b(GET|POST|PATCH|PUT|DELETE)\s+\//i },
    { name: "a host and port", pattern: /https?:\/\/[^\s)]+/i },
    { name: "a source file", pattern: /\b[\w/-]+\.(py|ts|tsx|sql)\b/i },
    { name: "a database or payload field", pattern: /\b[a-z][a-z0-9]*(_[a-z0-9]+)+\b/ },
    { name: "a setting or alert name", pattern: /\b[A-Z][A-Z0-9]*(_[A-Z0-9]+)+\b/ },
    { name: "a raw status", pattern: /\b(DONE|FAILED|QUEUED|RUNNING)\b/ },
  ];

  it("names no route, column, file, setting or status in any state", () => {
    const lines = [
      scanRunLine(null, {}),
      scanRunLine(null, { session: "2026-09-11" }),
      scanRunLine(run({ status: "QUEUED" }), {}),
      scanRunLine(run({ status: "RUNNING" }), {}),
      scanRunLine(run(), { now: finished + 60_000 }),
      scanRunLine(run({ funnel: { liquid: 41, candidates: {} } }), { now: finished + 60_000 }),
      scanRunLine(run({ funnel: null }), { now: null }),
      scanRunLine(run({ status: "FAILED", error: "BASKFY_SWING_SCAN is false" }), { now: null }),
    ];
    for (const line of lines) {
      for (const { name, pattern } of BANNED) {
        expect(pattern.exec(line), `${name} in "${line}"`).toBeNull();
      }
    }
  });
});
