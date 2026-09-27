import { describe, expect, it } from "vitest";

import { formatTradeDate } from "@/lib/format";
import {
  CLOSED_POLL_MS,
  EMPTY_LIVE_MARKS,
  LIVE_MAX_AGE_MS,
  LIVE_POLL_MS,
  type LiveMarks,
  formatQuoteTimeIST,
  formatTodayChange,
  livePollInterval,
  liveStatusLine,
  parseLiveMarks,
  quoteTitle,
  resolveLiveMarks,
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
    expect(live.quotes.RELIANCE).toEqual({
      lastPrice: 1520.4,
      prevClose: 1500,
      changePct: 1.36,
      stale: false,
      asOf: null,
    });
    expect(live.asOf).toBe("2026-09-18");
    expect(live.receivedAt).toBeNull();
  });

  // LV1 (27 Sep 2026): every quote carries the exchange's time and the server's stale verdict;
  // the answer says how much of the page it covers; the receipt time is the browser's.
  it("carries as_of, stale, coverage and the receipt time", () => {
    const live = parseLiveMarks(
      {
        live: true,
        reason: null,
        market_open: true,
        as_of: "2026-09-18",
        served_at: "2026-09-18T13:15:00+05:30",
        requested: 3,
        covered: 2,
        quotes: {
          RELIANCE: {
            last_price: "1520.40",
            prev_close: "1500.00",
            change_pct: "1.36",
            as_of: "2026-09-18T13:14:58+05:30",
            stale: false,
          },
          ILLIQ: {
            last_price: "12.50",
            prev_close: "12.50",
            change_pct: "0.00",
            as_of: "2026-09-18T12:40:00+05:30",
            stale: true,
          },
        },
      },
      1_700_000_000_000,
    );
    expect(live.quotes.RELIANCE).toMatchObject({ stale: false, asOf: "2026-09-18T13:14:58+05:30" });
    expect(live.quotes.ILLIQ).toMatchObject({ stale: true });
    expect(live.requested).toBe(3);
    expect(live.covered).toBe(2);
    expect(live.receivedAt).toBe(1_700_000_000_000);
  });

  it("never reports more coverage than it has quotes for", () => {
    const live = parseLiveMarks(
      {
        live: true,
        reason: null,
        market_open: true,
        as_of: "2026-09-18",
        requested: 1,
        covered: 5,
        quotes: { A: { last_price: "10", prev_close: "9", change_pct: "11.11" } },
      },
      1,
    );
    expect(live.covered).toBe(1);
    expect(live.requested).toBe(1);
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
      CUPID: { lastPrice: 291.5, prevClose: 284.03, changePct: 2.63, stale: false, asOf: null },
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

// LV1: an answer that stopped arriving must stop reading as live. Three missed polls, or a fetch
// that failed (react-query keeps the previous data on an error), and the page is back on the close.
describe("resolveLiveMarks", () => {
  const liveAnswer: LiveMarks = {
    ...EMPTY_LIVE_MARKS,
    liveOverlay: true,
    marketOpen: true,
    marks: { RELIANCE: 1520.4 },
    quotes: {
      RELIANCE: { lastPrice: 1520.4, prevClose: 1500, changePct: 1.36, stale: false, asOf: null },
    },
    receivedAt: 1_000_000,
    requested: 1,
    covered: 1,
  };
  const fresh = { isError: false, dataUpdatedAt: 1_000_000, now: 1_000_000 + 30_000 };

  it("keeps a fresh, successful answer", () => {
    expect(resolveLiveMarks(liveAnswer, fresh)).toBe(liveAnswer);
  });

  it("drops the overlay once the last good answer is older than 90 s", () => {
    expect(LIVE_MAX_AGE_MS).toBe(90_000);
    const old = resolveLiveMarks(liveAnswer, { ...fresh, now: 1_000_000 + LIVE_MAX_AGE_MS + 1 });
    expect(old.liveOverlay).toBe(false);
    expect(old.marks).toEqual({});
    expect(old.quotes).toEqual({});
    expect(old.reason).toBe("unavailable");
    expect(old.covered).toBe(0);
    expect(old.asOf).toBe(liveAnswer.asOf);
  });

  it("drops the overlay when the last fetch failed, however fresh the kept data looks", () => {
    const failed = resolveLiveMarks(liveAnswer, { ...fresh, isError: true });
    expect(failed.liveOverlay).toBe(false);
    expect(failed.reason).toBe("unavailable");
  });

  it("falls back to the parser's receipt time when react-query has none, and drops with neither", () => {
    expect(
      resolveLiveMarks(liveAnswer, { isError: false, dataUpdatedAt: 0, now: 1_000_000 + 1 }),
    ).toBe(liveAnswer);
    const noClock = resolveLiveMarks(
      { ...liveAnswer, receivedAt: null },
      { isError: false, dataUpdatedAt: 0, now: 5 },
    );
    expect(noClock.liveOverlay).toBe(false);
  });

  it("passes a not-live answer through untouched and answers empty before the first", () => {
    const closed = { ...EMPTY_LIVE_MARKS, reason: "market_closed" as const };
    expect(resolveLiveMarks(closed, fresh)).toBe(closed);
    expect(resolveLiveMarks(undefined, fresh)).toBe(EMPTY_LIVE_MARKS);
  });
});

describe("the status line counts coverage", () => {
  it("says how many rows are live when the answer covered fewer than the page asked for", () => {
    const partial: LiveMarks = {
      ...EMPTY_LIVE_MARKS,
      liveOverlay: true,
      marketOpen: true,
      marks: { A: 1 },
      requested: 50,
      covered: 42,
      asOf: "2026-09-18",
    };
    expect(liveStatusLine(partial, "2026-09-18")).toBe(
      `Live prices · 42 of 50 live · today's change vs previous close · ranks as of ${DAY}`,
    );
    expect(liveStatusLine({ ...partial, covered: 50 }, "2026-09-18")).toBe(
      `Live prices · today's change vs previous close · ranks as of ${DAY}`,
    );
  });
});

describe("the cell's tooltip", () => {
  const quote = { lastPrice: 1, prevClose: 1, changePct: 0, stale: false, asOf: null };

  it("renders the exchange time in IST whatever the browser's zone", () => {
    expect(formatQuoteTimeIST("2026-09-18T07:32:11Z")).toBe("13:02:11 IST");
    expect(formatQuoteTimeIST(null)).toBe("");
    expect(formatQuoteTimeIST("not a time")).toBe("");
  });

  it("names a stale print with its time, and says when the exchange sent no time", () => {
    expect(quoteTitle({ ...quote, stale: true, asOf: "2026-09-18T07:32:11Z" })).toBe(
      "Stale — last quote 13:02:11 IST",
    );
    expect(quoteTitle({ ...quote, stale: true })).toBe("Stale — last quote time unknown");
    expect(quoteTitle(quote)).toContain("the exchange sent no time");
    expect(quoteTitle({ ...quote, asOf: "2026-09-18T07:32:11Z" })).toBe("Live — Kite last price");
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
