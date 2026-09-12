import { existsSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { NAV_ITEMS } from "@/lib/nav";
import { screenDefaultView } from "@/lib/screens/feature-flags";

/**
 * AUDIT 4.10 — dead components, planned-nav machinery, and the legacy chip-filter flag are gone.
 */
const SRC = resolve(process.cwd(), "src");

describe("dead web plumbing removed", () => {
  for (const relative of [
    "components/portfolio/allocation-analytics.tsx",
    "components/portfolio/detail-screen.tsx",
    "components/explore/explore-filters.tsx",
  ] as const) {
    it(`deletes ${relative}`, () => {
      expect(existsSync(resolve(SRC, relative))).toBe(false);
    });
  }

  it("has no planned nav items", () => {
    expect(NAV_ITEMS.every((item) => item.status === "ready")).toBe(true);
  });

  it("does not export screenChipFiltersEnabled", async () => {
    const mod = await import("@/lib/screens/feature-flags");
    expect("screenChipFiltersEnabled" in mod).toBe(false);
    expect(screenDefaultView()).toBe("table");
  });

  it("screen-editor has no sr-only Apply test hook", async () => {
    const { readFileSync } = await import("node:fs");
    const source = readFileSync(resolve(SRC, "components/screens/screen-editor.tsx"), "utf8");
    expect(source).not.toMatch(/data-testid="apply-filters"/);
    expect(source).not.toMatch(/screenChipFiltersEnabled/);
    expect(source).not.toMatch(/FilterForm/);
  });
});
