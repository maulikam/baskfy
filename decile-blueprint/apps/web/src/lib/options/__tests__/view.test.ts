import { describe, expect, it } from "vitest";

import {
  checks,
  clockLabel,
  counterTrendBreaks,
  emptyReasonText,
  reasonText,
  roleLine,
  stateText,
  trigger,
} from "@/lib/options/view";

import {
  acMorning,
  afterClose,
  collectorOff,
  noScanYet,
  stale,
} from "./fixtures";

/** `docs/options/05` §2's clock table and the empty state's honesty, asserted as sentences. */
describe("the clock label", () => {
  it("is Live with the collector's minute while the session is open and the rows are today's", () => {
    expect(clockLabel(acMorning)).toEqual({
      text: "Live · 13:14",
      tone: "positive",
      live: true,
    });
  });

  it("turns amber and says stale beyond two minutes", () => {
    expect(clockLabel(stale)).toEqual({
      text: "Live · 13:14 · stale",
      tone: "warning",
      live: true,
    });
  });

  it("is As of close outside hours, naming the session", () => {
    expect(clockLabel(afterClose).text).toBe(
      "As of close, Tue 22 Sep · market closed",
    );
  });

  it("names yesterday's session when today has not been scanned yet", () => {
    expect(clockLabel(noScanYet).text).toBe(
      "As of close, Mon 21 Sep · market open",
    );
  });

  it("claims nothing when nothing has been scanned", () => {
    expect(clockLabel(collectorOff)).toEqual({
      text: "No scan yet",
      tone: "neutral",
      live: false,
    });
  });
});

describe("the empty state says why", () => {
  it("names the collector when it is off", () => {
    expect(emptyReasonText("collector_off")).toMatch(
      /options collector is off/,
    );
  });

  it("names the scan switch, the clock, or never — and is silent when rows are shown", () => {
    expect(emptyReasonText("scan_off")).toMatch(/scan is switched off/);
    expect(emptyReasonText("no_scan_yet_today")).toMatch(/No scan yet today/);
    expect(emptyReasonText("never_scanned")).toMatch(
      /never|No options scan has ever run/,
    );
    expect(emptyReasonText(null)).toBeNull();
  });
});

describe("codes are words", () => {
  it("translates states and reasons, and sentence-cases a code it has not met", () => {
    expect(stateText("WOULD_SKIP")).toEqual({
      label: "Would skip",
      tone: "warning",
    });
    expect(stateText("SOMETHING_NEW").label).toBe("Something new");
    expect(reasonText("ER_TOO_HIGH")).toBe(
      "The morning is trending, not ranging",
    );
    expect(reasonText("A_FUTURE_CODE")).toBe("A future code");
  });

  it("writes each sleeve's role for the header", () => {
    expect(roleLine(acMorning.roles[0]!)).toBe("O1-M: next Tue 29 Sep");
    expect(roleLine(acMorning.roles[1]!)).toBe("O1-W: today");
  });
});

describe("each sleeve's numbers", () => {
  const scan = (sleeve: string) =>
    acMorning.scans.find((row) => row.sleeve === sleeve)!;

  it("O1: every filter against its threshold, failing ones marked", () => {
    const rows = checks(scan("O1W"));
    expect(rows.map((row) => [row.label, row.ok])).toEqual([
      ["Opening gap", true],
      ["Morning range", false],
      ["Still inside the opening range", false],
      ["Efficiency ratio", false],
    ]);
    expect(rows[1]!.value).toBe("0.94%");
  });

  it("O2: the trigger level and the distance to it, in points and percent", () => {
    expect(trigger(scan("O2"))).toEqual({
      level: "25,162.55",
      points: "31.45",
      pct: "0.13%",
    });
    expect(counterTrendBreaks(scan("O2"))).toEqual([
      { time: "10:04", close: "25,040.15", direction: "down" },
    ]);
  });
});
