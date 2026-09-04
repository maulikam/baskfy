import { describe, expect, it } from "vitest";

import { isMarketOpen } from "@/lib/market/session";

/** 09:15–15:30 IST, mirroring `market_hours_cb.py`. Built in UTC so the test is timezone-proof. */
const utc = (iso: string) => new Date(iso);

describe("isMarketOpen", () => {
  it("is open inside the session on a weekday", () => {
    // 2026-09-02 is a Wednesday. 06:00Z = 11:30 IST.
    expect(isMarketOpen(utc("2026-09-02T06:00:00Z"))).toBe(true);
  });

  it("is open exactly at the bell, both ends", () => {
    // 03:45Z = 09:15 IST, 10:00Z = 15:30 IST. Inclusive, as the Python does.
    expect(isMarketOpen(utc("2026-09-02T03:45:00Z"))).toBe(true);
    expect(isMarketOpen(utc("2026-09-02T10:00:00Z"))).toBe(true);
  });

  it("is shut a minute either side", () => {
    expect(isMarketOpen(utc("2026-09-02T03:44:00Z"))).toBe(false);
    expect(isMarketOpen(utc("2026-09-02T10:01:00Z"))).toBe(false);
  });

  it("is shut at the weekend", () => {
    // 2026-09-05 is a Saturday, 2026-09-06 a Sunday — both mid-session by clock.
    expect(isMarketOpen(utc("2026-09-05T06:00:00Z"))).toBe(false);
    expect(isMarketOpen(utc("2026-09-06T06:00:00Z"))).toBe(false);
  });

  it("reads IST regardless of the viewer's own timezone", () => {
    // 20:00Z on Tuesday is 01:30 IST Wednesday — a weekday by the clock, and shut.
    expect(isMarketOpen(utc("2026-09-01T20:00:00Z"))).toBe(false);
  });
});
