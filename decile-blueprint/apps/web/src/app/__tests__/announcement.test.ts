/**
 * The banner cannot be stale, whatever else it is.
 *
 * `app/(app)/layout.tsx` carried this in JSX until 26 August 2026:
 *
 * > **December 2026 update:** split- and bonus-adjusted history, a longer backfill, and backtests
 * > are on the way.
 *
 * Served in August, announcing December, and calling three shipped features forthcoming. Nothing
 * could have caught it: prose is not type-checked and a hard-coded date is not a value anything
 * compares against. The fix was to make the expiry a **field**, so "is this still true?" becomes a
 * question a test can ask.
 */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { ANNOUNCEMENT, visibleAnnouncement, type Announcement } from "@/lib/marketing/announcement";

const LAYOUT = readFileSync(resolve(process.cwd(), "src/app/(app)/layout.tsx"), "utf8");

function announcement(over: Partial<Announcement> = {}): Announcement {
  return {
    id: "example",
    lead: "Something happened:",
    body: "and here is what it was.",
    action: { label: "Read it", href: "/blog" },
    until: "2099-01-01",
    ...over,
  };
}

describe("whatever ships is not already expired", () => {
  it("the shipped announcement is either absent or still in date", () => {
    // The one assertion that would have caught the December banner. It runs on every CI run, so
    // an announcement outlives its own relevance by at most one merge.
    expect(visibleAnnouncement(new Date(), ANNOUNCEMENT)).toBe(ANNOUNCEMENT);
  });

  it("no dated announcement is hard-coded in the layout any more", () => {
    // The specific rot, and the general shape of it: a month name sitting in JSX.
    expect(LAYOUT).not.toContain("December 2026 update");
    for (const month of ["January", "February", "March", "December"]) {
      expect(LAYOUT).not.toMatch(new RegExp(`${month}\\s+\\d{4}`));
    }
  });
});

describe("expiry decides visibility", () => {
  it("shows a message before its date", () => {
    const live = announcement({ until: "2026-12-01" });
    expect(visibleAnnouncement(new Date("2026-11-30T12:00:00Z"), live)).toBe(live);
  });

  it("hides it on the day itself", () => {
    // Exclusive: `until` is the first day it is no longer true, which is the reading least likely
    // to leave something up one day too long.
    const live = announcement({ until: "2026-12-01" });
    expect(visibleAnnouncement(new Date("2026-12-01T00:00:00Z"), live)).toBeNull();
  });

  it("hides it long after", () => {
    const live = announcement({ until: "2026-01-01" });
    expect(visibleAnnouncement(new Date("2026-08-26T00:00:00Z"), live)).toBeNull();
  });

  it("hides a record whose date cannot be parsed", () => {
    // A typo in the date must fail closed. Showing a banner forever because "soon" is not a date
    // is exactly the failure this module exists to prevent.
    expect(visibleAnnouncement(new Date(), announcement({ until: "soon" }))).toBeNull();
  });

  it("null is a real answer, not an unfinished one", () => {
    expect(visibleAnnouncement(new Date(), null)).toBeNull();
  });
});
