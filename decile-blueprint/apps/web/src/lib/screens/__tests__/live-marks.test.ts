import { describe, expect, it } from "vitest";

import { withLivePrice } from "@/lib/screens/live-marks";

describe("withLivePrice", () => {
  it("stamps last_price from the live map and leaves the close alone", () => {
    const row = { symbol: "cupid", close_raw: 284.03, rank: 1 };
    expect(withLivePrice(row, { CUPID: 291.5 })).toEqual({
      symbol: "cupid",
      close_raw: 284.03,
      rank: 1,
      last_price: 291.5,
    });
  });

  it("leaves the row untouched when there is no mark", () => {
    const row = { symbol: "CUPID", close_raw: 284.03, rank: 1 };
    expect(withLivePrice(row, {})).toBe(row);
  });
});
