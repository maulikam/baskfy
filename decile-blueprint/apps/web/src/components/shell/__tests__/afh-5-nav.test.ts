import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { SECTION_TABS } from "@/lib/nav";

describe("AFH 5.5–5.7 / 5.12 nav and build copy", () => {
  it("renames Build's Your screens and prompts for a name on save", () => {
    const source = readFileSync(
      join(__dirname, "..", "..", "screens", "screens-list.tsx"),
      "utf8",
    );
    expect(source).toContain('title="Your screens"');
    expect(source).not.toContain('title="Your baskets"');
    expect(source).toContain("Name this screen");
    expect(source).toContain('data-testid="new-screen-name"');
  });

  it("drops operator sleeves from the consumer Build tab row", () => {
    expect(SECTION_TABS.build.map((t) => t.label)).toEqual([
      "Screens",
      "Backtests",
      "Create",
      "Overlap",
    ]);
  });

  it("points Discover's saved tab at Watchlist", () => {
    const saved = SECTION_TABS.discover.find((t) => t.label === "Watchlist");
    expect(saved?.href).toBe("/portfolio/watchlist");
    expect(SECTION_TABS.discover.map((t) => t.label)).not.toContain("Saved");
  });

  it("gates Real money and Operator user-menu groups on isStaff", () => {
    const source = readFileSync(join(__dirname, "..", "user-menu.tsx"), "utf8");
    expect(source).toContain("STAFF_ONLY_GROUPS");
    expect(source).toContain("Real money");
    expect(source).toContain("Operator");
  });
});
