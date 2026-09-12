import { describe, expect, it } from "vitest";

import { sectionTabIsActive } from "@/components/shell/section-tab-active";
import { SECTION_TABS } from "@/lib/nav";

/**
 * AUDIT 1.7 — `/build/backtests` used to light both Screens and Backtests because every tab
 * used `startsWith`. Longest-match leaves exactly one active.
 */
describe("sectionTabIsActive", () => {
  const buildHrefs = SECTION_TABS.build.map((tab) => tab.href);
  const swingHrefs = SECTION_TABS.swing.map((tab) => tab.href);
  const discoverHrefs = SECTION_TABS.discover.map((tab) => tab.href);

  it("lights only Backtests on /build/backtests, not Screens", () => {
    expect(sectionTabIsActive("/build/backtests", "/build", buildHrefs)).toBe(false);
    expect(sectionTabIsActive("/build/backtests", "/build/backtests", buildHrefs)).toBe(true);
  });

  it("lights only Screens on /build itself", () => {
    expect(sectionTabIsActive("/build", "/build", buildHrefs)).toBe(true);
    expect(sectionTabIsActive("/build", "/build/backtests", buildHrefs)).toBe(false);
  });

  it("lights only Watchlist on /swing/watchlist, not Setups", () => {
    expect(sectionTabIsActive("/swing/watchlist", "/swing", swingHrefs)).toBe(false);
    expect(sectionTabIsActive("/swing/watchlist", "/swing/watchlist", swingHrefs)).toBe(true);
  });

  it("lights For you only on exact /discover, not on /discover/all", () => {
    expect(sectionTabIsActive("/discover", "/discover", discoverHrefs)).toBe(true);
    expect(sectionTabIsActive("/discover/all", "/discover", discoverHrefs)).toBe(false);
    expect(sectionTabIsActive("/discover/all", "/discover/all", discoverHrefs)).toBe(true);
  });
});
