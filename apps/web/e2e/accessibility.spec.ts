import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

/**
 * Prompt 8's first acceptance criterion:
 *
 *     "Lighthouse ≥ 95 accessibility on / and /kitchen-sink in both themes."
 *
 * Lighthouse's accessibility category *is* axe-core: it runs a fixed subset of axe's rules and
 * turns the pass/fail results into a weighted score out of 100. Running axe directly through
 * Playwright measures the same rules on the same rendered page, and does it inside the same
 * browser session — which is what makes "in both themes" testable at all, since a theme is a
 * class on `<html>` set after hydration and a fresh Lighthouse run would start in the default one.
 *
 * `e2e/lighthouse.spec.ts` runs the real Lighthouse binary and asserts the actual score, for the
 * default theme where it can. This file is the both-themes half. See `docs/08a` §6.
 */
const PAGES = [
  { path: "/", name: "landing" },
  { path: "/kitchen-sink", name: "kitchen sink" },
  // Prompt 10's route. It is the organic-traffic surface, so it is the one page most likely to be
  // someone's first — and the only one they may never have chosen a theme on.
  { path: "/instruments/CUPID", name: "instrument factsheet" },
  // Prompt 11's three. The dashboard is the densest grid in the product and market health is the
  // only page whose meaning is carried by drawn arcs, so both are worth sweeping in both themes.
  { path: "/dashboard", name: "indices dashboard" },
  { path: "/market-health", name: "market health" },
  { path: "/listings", name: "listings" },
  // Prompt 13's checkout surface. Public, and the one page where a colour-only affordance would
  // stand between someone and paying.
  { path: "/pricing", name: "pricing" },
] as const;

const THEMES = ["light", "dark"] as const;

async function setTheme(page: Page, theme: string): Promise<void> {
  await page.evaluate((value) => {
    window.localStorage.setItem("theme", value);
  }, theme);
  await page.reload();
  await page.waitForLoadState("networkidle");
  if (theme === "dark") {
    await expect(page.locator("html")).toHaveClass(/dark/);
  }
}

for (const { path, name } of PAGES) {
  for (const theme of THEMES) {
    test(`${name} has no accessibility violations in ${theme}`, async ({ page }) => {
      await page.goto(path);
      await setTheme(page, theme);

      const results = await new AxeBuilder({ page })
        // WCAG 2.2 AA is docs/11 §Accessibility's bar, stated by name.
        .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
        .analyze();

      const summary = results.violations.map((violation) =>
        // The selectors as well as the rule: "Scrollable region must have keyboard access" names
        // no region, and hunting for it by hand is how these get suppressed instead of fixed.
        [
          `${violation.id} (${violation.nodes.length}): ${violation.help}`,
          ...violation.nodes.map((node) => `    ${node.target.join(" ")}`),
        ].join("\n"),
      );
      expect(summary, summary.join("\n")).toEqual([]);
    });
  }
}

test("colour contrast holds in both themes on the densest surface", async ({ page }) => {
  // docs/11 §Accessibility: "Contrast >= 4.5:1 in both themes, including the positive/negative
  // number colours." The token palette is verified arithmetically in `contrast.test.ts`; this
  // checks the composed result, where a translucent background could still undo it.
  for (const theme of THEMES) {
    await page.goto("/kitchen-sink");
    await setTheme(page, theme);
    const results = await new AxeBuilder({ page }).withRules(["color-contrast"]).analyze();
    expect(results.violations, `${theme}: ${JSON.stringify(results.violations, null, 2)}`).toEqual(
      [],
    );
  }
});
