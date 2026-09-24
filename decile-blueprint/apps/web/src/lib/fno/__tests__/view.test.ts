import { describe, expect, it } from "vitest";

import { filterRows, sortRows } from "@/components/fno/info-table";
import { widths } from "@/components/fno/overnight";

import { asOfClose, liveLabel, markedAtSettle, stateText } from "../view";
import { info } from "./fixtures";

/** `docs/fno/05` §2-§3's labels, character for character, and `04` §5's table behaviour. */
describe("the FO clock labels are 05's", () => {
  it("writes the three labels exactly", () => {
    expect(asOfClose("2026-09-22")).toBe("As of close, Tue 22 Sep");
    expect(markedAtSettle("2026-09-22")).toBe("Marked at settle, 22 Sep");
    expect(liveLabel("NIFTY", "13:14")).toBe("NIFTY live 13:14");
  });

  it("says so when there is no date rather than inventing one", () => {
    expect(asOfClose(null)).toBe("No close read yet");
    expect(markedAtSettle(null)).toBe("Not marked yet");
  });

  it("names every 04 §8 scan state in words and never hides an unknown one", () => {
    expect(stateText("NOT_ENTRY_DAY").label).toBe("Not an entry day");
    expect(stateText("REJECTED_SIZE").label).toBe("Rejected: size");
    expect(stateText("SOMETHING_NEW").label).toBe("SOMETHING_NEW");
  });
});

describe("the Stock F&O table (04 §5)", () => {
  it("sorts by futures turnover by default order, nulls last either way", () => {
    expect(sortRows(info.rows, "turnover", true).map((r) => r.symbol)).toEqual([
      "RELIANCE",
      "TCS",
    ]);
    expect(sortRows(info.rows, "iv", true).map((r) => r.symbol)).toEqual([
      "RELIANCE",
      "TCS",
    ]);
    expect(sortRows(info.rows, "iv", false).map((r) => r.symbol)).toEqual([
      "RELIANCE",
      "TCS",
    ]);
    expect(sortRows(info.rows, "oi", false).map((r) => r.symbol)).toEqual([
      "TCS",
      "RELIANCE",
    ]);
  });

  it("filters on the ban list", () => {
    expect(filterRows(info.rows, "banned").map((r) => r.symbol)).toEqual([
      "TCS",
    ]);
    expect(filterRows(info.rows, "not_banned").map((r) => r.symbol)).toEqual([
      "RELIANCE",
    ]);
    expect(filterRows(info.rows, "all")).toHaveLength(2);
  });
});

describe("the proposed condor's widths", () => {
  it("are the distances from each short strike to its wing", () => {
    expect(
      widths([
        { role: "LONG_PUT", strike: "51000", option_type: "PE" },
        { role: "SHORT_PUT", strike: "52000", option_type: "PE" },
        { role: "SHORT_CALL", strike: "55500", option_type: "CE" },
        { role: "LONG_CALL", strike: "56500", option_type: "CE" },
      ]),
    ).toEqual({ put: 1000, call: 1000 });
  });
});
