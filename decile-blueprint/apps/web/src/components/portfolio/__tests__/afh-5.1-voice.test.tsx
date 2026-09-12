import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { SECTION_TABS } from "@/lib/nav";

/**
 * AFH 5.1 — one portfolio voice: Overview default, Details = command centre, same tabs,
 * whole-rupee format on the command surface.
 */
const ROOT = join(__dirname, "..");
const APP_PORTFOLIOS = join(
  __dirname,
  "..",
  "..",
  "..",
  "app",
  "(app)",
  "portfolio",
  "portfolios",
  "page.tsx",
);

describe("AFH 5.1 portfolio voice", () => {
  it("labels the command-centre tab Details, not Portfolios", () => {
    const tab = SECTION_TABS.portfolio.find((item) => item.href === "/portfolio/portfolios");
    expect(tab?.label).toBe("Details");
  });

  it("renders the same portfolio section tabs on the Details page", () => {
    const page = readFileSync(APP_PORTFOLIOS, "utf8");
    expect(page).toContain("SectionTabs");
    expect(page).toContain('section="portfolio"');
  });

  it("titles the command header Details, not Portfolio Command Center", () => {
    const header = readFileSync(join(ROOT, "command", "command-header.tsx"), "utf8");
    /* Matched against the `<h1>` rather than as the bare text `>Details<`: the heading's class
       list grew past the print width and Prettier wrapped the element, which broke the old
       matcher without changing a character a reader sees. The title is the spec; its formatting
       is not. */
    expect(header).toMatch(/<h1[^>]*>\s*Details\s*<\/h1>/);
    expect(header).not.toContain("Portfolio Command Center");
  });

  it("formats command-centre rupees without paise", () => {
    const band = readFileSync(join(ROOT, "command", "metric-band.tsx"), "utf8");
    expect(band).toContain("formatRupees(metric.value, { decimals: 0 })");
  });
});
