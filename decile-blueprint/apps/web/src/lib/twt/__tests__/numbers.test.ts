import { describe, expect, it } from "vitest";

import { position, today } from "@/lib/twt/__tests__/fixtures";
import {
  asPercent,
  distanceToTriggerPct,
  figure,
  noFigure,
  percent,
  ratioAsUpliftPct,
  subtractDecimals,
  toCrore,
  toneOf,
} from "@/lib/twt/numbers";
import { positionView, todayView } from "@/lib/twt/view";

/**
 * The twt arithmetic, on decimal strings — house rule 9, and the one-conversion rule.
 *
 * These are the assertions worth having, because each one pins a mistake that has actually been
 * made in this repository within the last week rather than a property that was never at risk.
 */

describe("twt money never becomes a float", () => {
  it("subtracts at the scale it was given, without a float's rounding", () => {
    expect(subtractDecimals("441.20", "364.00")).toBe("77.20");
    expect(subtractDecimals("0.1", "0.3")).toBe("-0.2");
    expect(subtractDecimals("1000000.05", "0.10")).toBe("999999.95");
  });

  it("refuses a value that is not a decimal rather than coercing it", () => {
    expect(subtractDecimals("1.00", "one")).toBeNull();
    expect(subtractDecimals(null, "1.00")).toBeNull();
  });

  it("turns rupees into crore exactly", () => {
    expect(toCrore("341200000")).toBe("34.12");
    expect(toCrore("4100000")).toBe("0.41");
    expect(toCrore(null)).toBeNull();
  });
});

describe("twt converts a rate exactly once", () => {
  /**
   * The defect this pins, by name: on 11 Sep 2026 the portfolio band rendered a 1.99% day as
   * 0.0199% because the component scaled a fraction the builder had already scaled. The unit is
   * in the field name in the payload and the multiplication happens in one function, so a second
   * one would have to be written on purpose.
   */
  it("reads a stored fraction as the percentage it is: 0.019900 is 1.99%", () => {
    expect(asPercent("0.019900")).toBe("1.99");
    expect(asPercent("0.420700")).toBe("42.07");
    expect(asPercent("-0.087500")).toBe("-8.75");
    expect(asPercent(null)).toBeNull();
  });

  it("does not scale a figure that is already a percentage", () => {
    const view = positionView(position());
    expect(view.unrealisedPct.value).toBe("+42.1%");
    expect(view.unrealisedPct.value).not.toBe("+0.4%");
  });

  it("reads a ratio to the three-month low as the uplift it describes", () => {
    expect(ratioAsUpliftPct("1.4200")).toBe("42.00");
    expect(ratioAsUpliftPct("1.3000")).toBe("30.00");
  });
});

describe("twt measures the room under a position", () => {
  it("computes the distance to the trigger as a share of the last price", () => {
    // (441.20 − 364.00) / 441.20 × 100
    expect(distanceToTriggerPct("441.20", "364.00")).toBe("17.50");
    expect(percent("17.50", 1)).toBe("17.5%");
  });

  it("has no distance when there is no trigger, and says so rather than returning zero", () => {
    expect(distanceToTriggerPct("441.20", null)).toBeNull();
    const view = positionView(position({ gtt_id: null, gtt_trigger: null }));
    expect(view.distanceToTrigger.value).toBeNull();
    expect(view.distanceToTrigger.unavailable).toMatch(/No stop resting/i);
  });

  it("has no distance when the market has not priced the name today", () => {
    const view = positionView(position({ last_price: null }));
    expect(view.distanceToTrigger.value).toBeNull();
    expect(view.distanceToTrigger.unavailable).toMatch(/No live price/i);
  });
});

describe("a twt figure is a value or a reason, never neither", () => {
  it("supplies a reason for an absent value even when the caller forgot to think of one", () => {
    expect(figure(null, "Not computed for this session").unavailable).toBe(
      "Not computed for this session",
    );
    expect(figure("", "Not computed for this session").value).toBeNull();
    expect(noFigure("No live price for this name yet").value).toBeNull();
  });

  it("never colours an absence green or red", () => {
    expect(toneOf(null)).toBe("");
    expect(toneOf("0.00")).toBe("");
    expect(toneOf("42.07")).toBe("text-positive");
    expect(toneOf("-8.75")).toBe("text-negative");
  });
});

describe("the twt hub ranks by the liquidity it can actually use", () => {
  it("sorts the quiet names by turnover, heaviest first, and puts the unknown last", () => {
    const view = todayView(
      today({
        tight: [
          { ...today().tight[0]!, instrument_id: 1, symbol: "MID", turnover_avg_20: 50_000_000 },
          { ...today().tight[0]!, instrument_id: 2, symbol: "UNKNOWN", turnover_avg_20: null },
          { ...today().tight[0]!, instrument_id: 3, symbol: "BIG", turnover_avg_20: 900_000_000 },
        ],
      }),
    );
    expect(view.tight.map((row) => row.symbol)).toEqual(["BIG", "MID", "UNKNOWN"]);
  });

  it("does not throw when turnover is the integer the API actually sends", () => {
    /* Regression for digest 3692141499 on the box, 12 Sep 2026: compareTurnover called
       `.split` on a JSON number and the RSC render became "Three weeks tight could not be read"
       over a `/twt/today` that had answered 200 with 58 names. */
    expect(() =>
      todayView(
        today({
          tight: [
            { ...today().tight[0]!, instrument_id: 1, symbol: "A", turnover_avg_20: 5_442_855_635 },
            { ...today().tight[0]!, instrument_id: 2, symbol: "B", turnover_avg_20: 120_000_000 },
          ],
        }),
      ),
    ).not.toThrow();
  });
});
