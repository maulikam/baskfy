import { describe, expect, it } from "vitest";

import { formatTradeDate } from "@/lib/format";
import {
  CLOSED_POLL_MS,
  EMPTY_LIVE_MARKS,
  LIVE_POLL_MS,
  formatTodayChange,
  livePollInterval,
  liveStatusLine,
  parseLiveMarks,
  withLivePrice,
} from "@/lib/screens/live-marks";

const DAY = formatTradeDate("2026-09-18");

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

// The screens' live overlay (Maulik, 21 Sep 2026: "screens should have live data").
describe("parseLiveMarks", () => {
  it("reads decimal strings into display numbers while live", () => {
    const live = parseLiveMarks({
      live: true,
      live_overlay: true,
      reason: null,
      market_open: true,
      as_of: "2026-09-18",
      marks: { RELIANCE: "1520.40" },
      quotes: { RELIANCE: { last_price: "1520.40", prev_close: "1500.00", change_pct: "1.36" } },
    });
    expect(live.liveOverlay).toBe(true);
    expect(live.reason).toBeNull();
    expect(live.quotes.RELIANCE).toEqual({ lastPrice: 1520.4, prevClose: 1500, changePct: 1.36 });
    expect(live.asOf).toBe("2026-09-18");
  });

  it("carries the reason and no prices when the market is closed", () => {
    const live = parseLiveMarks({
      live: false,
      live_overlay: false,
      reason: "market_closed",
      market_open: false,
      as_of: "2026-09-18",
      marks: {},
      quotes: {},
    });
    expect(live.liveOverlay).toBe(false);
    expect(live.reason).toBe("market_closed");
    expect(live.quotes).toEqual({});
    expect(live.marks).toEqual({});
  });
});

describe("withLivePrice and today's change", () => {
  it("stamps today's change beside the live price, and nothing else", () => {
    const row = { symbol: "CUPID", close_raw: 284.03, rank: 3, sorting_factor: 1.2 };
    const out = withLivePrice(row, { CUPID: 291.5 }, {
      CUPID: { lastPrice: 291.5, prevClose: 284.03, changePct: 2.63 },
    });
    expect(out).toEqual({ ...row, last_price: 291.5, live_change_pct: 2.63 });
  });

  it("always signs the change", () => {
    expect(formatTodayChange(1.36)).toBe("+1.36% today");
    expect(formatTodayChange(-0.4)).toBe("−0.40% today");
    expect(formatTodayChange(0)).toBe("±0.00% today");
  });
});

describe("liveStatusLine", () => {
  const base = { ...EMPTY_LIVE_MARKS, asOf: "2026-09-18" };

  it("says which numbers are live and keeps the ranks' date", () => {
    expect(
      liveStatusLine({ ...base, liveOverlay: true, marketOpen: true, marks: { A: 1 } }, "2026-09-18"),
    ).toBe(`Live prices · today's change vs previous close · ranks as of ${DAY}`);
  });

  it("names the close and the reason when not live", () => {
    expect(liveStatusLine({ ...base, reason: "market_closed" }, "2026-09-18")).toBe(
      `Close as of ${DAY} · market closed`,
    );
    expect(liveStatusLine({ ...base, reason: "no_session", marketOpen: true }, null)).toBe(
      `Close as of ${DAY} · no Kite session today, so no live prices`,
    );
  });
});

describe("livePollInterval", () => {
  it("polls every 30 s only while the market is open", () => {
    expect(livePollInterval({ ...EMPTY_LIVE_MARKS, marketOpen: true })).toBe(LIVE_POLL_MS);
    expect(LIVE_POLL_MS).toBe(30_000);
    expect(livePollInterval({ ...EMPTY_LIVE_MARKS, marketOpen: false })).toBe(CLOSED_POLL_MS);
    expect(livePollInterval(undefined)).toBe(CLOSED_POLL_MS);
    expect(CLOSED_POLL_MS).toBeGreaterThan(LIVE_POLL_MS);
  });
});
