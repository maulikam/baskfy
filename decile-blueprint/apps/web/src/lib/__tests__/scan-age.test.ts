import { describe, expect, it } from "vitest";

import { RELATIVE_HORIZON_MS, relativeIST, stampIST, whenPhrase } from "@/lib/scan-age";

/**
 * "How long ago did this scan run" — the phrase every sleeve's scan line is built on.
 *
 * The spec these assert is `docs/twt/05` §1's register applied to a timestamp: a reader wants
 * "12 minutes ago", not a stamp they have to subtract from their own clock, and not "17,412
 * minutes ago" once the subtraction stops being interesting. Past a day the IST wall clock is the
 * more useful of the two and the phrase gives way to it.
 */

const AT = "2026-09-12T11:29:00+00:00"; // 16:59 IST, the newest real run on the box
const at = new Date(AT).getTime();

describe("relativeIST says how long ago, in the buckets a person speaks in", () => {
  it("says just now inside the first three-quarters of a minute", () => {
    expect(relativeIST(AT, at + 1_000)).toBe("just now");
    expect(relativeIST(AT, at + 44_000)).toBe("just now");
  });

  it("says a minute rather than 1 minutes", () => {
    expect(relativeIST(AT, at + 60_000)).toBe("a minute ago");
  });

  it("says whole minutes for the first hour", () => {
    expect(relativeIST(AT, at + 12 * 60_000)).toBe("12 minutes ago");
    expect(relativeIST(AT, at + 59 * 60_000)).toBe("59 minutes ago");
  });

  it("says an hour, then whole hours", () => {
    expect(relativeIST(AT, at + 61 * 60_000)).toBe("an hour ago");
    expect(relativeIST(AT, at + 5 * 3_600_000)).toBe("5 hours ago");
  });

  /* A clock that disagrees by a few seconds is not a scan that has not happened yet. */
  it("says just now for a stamp slightly in the future rather than a negative age", () => {
    expect(relativeIST(AT, at - 4_000)).toBe("just now");
  });

  it("gives up past a day, so the caller falls back to the stamp", () => {
    expect(relativeIST(AT, at + RELATIVE_HORIZON_MS - 1)).toBe("24 hours ago");
    expect(relativeIST(AT, at + RELATIVE_HORIZON_MS + 1)).toBeNull();
  });

  /* `null` is the server's reading: no clock, so no age — see `use-ticking-now.ts`. */
  it("says nothing without a clock to compare against, and nothing without a stamp", () => {
    expect(relativeIST(AT, null)).toBeNull();
    expect(relativeIST(null, at)).toBeNull();
    expect(relativeIST("not a date", at)).toBeNull();
  });
});

describe("stampIST is the fallback, and never a dash in the middle of a sentence", () => {
  it("names the wall clock and its zone", () => {
    expect(stampIST(AT)).toBe("at 12 Sept 2026, 16:59 IST");
  });

  it("answers null rather than the empty cell for something it cannot parse", () => {
    expect(stampIST("not a date")).toBeNull();
    expect(stampIST(null)).toBeNull();
  });
});

describe("whenPhrase picks the more useful of the two", () => {
  it("prefers the age while the run is recent", () => {
    expect(whenPhrase(AT, at + 12 * 60_000)).toBe("12 minutes ago");
  });

  it("falls back to the stamp once it is not, and on the server's pass", () => {
    expect(whenPhrase(AT, at + 3 * RELATIVE_HORIZON_MS)).toBe("at 12 Sept 2026, 16:59 IST");
    expect(whenPhrase(AT, null)).toBe("at 12 Sept 2026, 16:59 IST");
  });

  it("answers null when there is no timestamp at all", () => {
    expect(whenPhrase(null, at)).toBeNull();
  });
});
