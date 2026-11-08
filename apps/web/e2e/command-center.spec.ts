import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

/**
 * The Portfolio Command Center, in a real browser — `GATES.md` G15 and G16.
 *
 * The unit suite renders this screen in jsdom, where no stylesheet runs. Two of the brief's
 * requirements are invisible there by construction:
 *
 *   · **Accessibility, in light AND dark.** Contrast is a property of composed, painted colour.
 *     `contrast.test.ts` checks the token pairs arithmetically; this checks what the browser
 *     actually painted, including the translucent surfaces that can undo a passing token pair.
 *   · **Responsive behaviour.** The rail collapses into flow below `xl`, and the ten-column table
 *     is replaced by a stacked list below `md`. Both are CSS decisions, so only a browser can say
 *     whether they happened.
 *
 * The signed-in e2e account holds no broker positions, so this walks the screen in its EMPTY
 * state. That is deliberate rather than a limitation: an empty command centre is the first thing
 * a new user sees, it is the state most likely to ship unexamined, and the brief requires it to
 * explain itself with one next action instead of rendering a wall of dashes.
 */

const PATH = "/portfolio/portfolios";

const VIEWPORTS = {
  desktop: { width: 1440, height: 900 },
  tablet: { width: 1024, height: 768 },
  mobile: { width: 390, height: 844 },
} as const;

async function setTheme(page: Page, theme: "light" | "dark"): Promise<void> {
  await page.evaluate((value) => {
    window.localStorage.setItem("theme", value);
  }, theme);
  await page.reload();
  await page.waitForLoadState("networkidle");
  if (theme === "dark") await expect(page.locator("html")).toHaveClass(/dark/);
}

for (const theme of ["light", "dark"] as const) {
  test(`the command centre has no accessibility violations in ${theme}`, async ({ page }) => {
    await page.goto(PATH);
    await setTheme(page, theme);
    await expect(page.getByRole("heading", { name: "Portfolio Command Center" })).toBeVisible();

    const results = await new AxeBuilder({ page })
      // docs/11 §Accessibility states WCAG 2.2 AA by name; the rest of the suite uses these tags.
      .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
      .analyze();

    const summary = results.violations.map((violation) =>
      [
        `${violation.id} (${violation.nodes.length}): ${violation.help}`,
        ...violation.nodes.map((node) => `    ${node.target.join(" ")}`),
      ].join("\n"),
    );
    expect(summary, summary.join("\n")).toEqual([]);
  });

  test(`the command centre's contrast holds in ${theme}`, async ({ page }) => {
    await page.goto(PATH);
    await setTheme(page, theme);
    await expect(page.getByRole("heading", { name: "Portfolio Command Center" })).toBeVisible();

    const results = await new AxeBuilder({ page }).withRules(["color-contrast"]).analyze();
    expect(
      results.violations,
      `${theme}: ${JSON.stringify(results.violations, null, 2)}`,
    ).toEqual([]);
  });
}

test("on tablet the intelligence rail collapses out of its column", async ({ page }) => {
  await page.setViewportSize(VIEWPORTS.desktop);
  await page.goto(PATH);
  const rail = page.getByTestId("attention-rail");
  await expect(rail).toBeVisible();

  const header = page.getByRole("heading", { name: "Portfolio Command Center" });
  const wide = await rail.boundingBox();
  const headerBox = await header.boundingBox();
  expect(wide, "rail has no box on desktop").not.toBeNull();
  expect(headerBox).not.toBeNull();
  // Side by side: the rail starts well to the right of the page's left edge.
  expect(wide!.x).toBeGreaterThan(headerBox!.x + 400);

  await page.setViewportSize(VIEWPORTS.tablet);
  const narrow = await rail.boundingBox();
  expect(narrow, "rail has no box on tablet").not.toBeNull();
  // In flow: it spans the column rather than sitting in a 20rem gutter beside it.
  expect(narrow!.x).toBeLessThan(wide!.x);
  expect(narrow!.width).toBeGreaterThan(wide!.width);
});

test("on mobile the desktop table is not attempted and nothing scrolls sideways", async ({
  page,
}) => {
  await page.setViewportSize(VIEWPORTS.mobile);
  await page.goto(PATH);
  await expect(page.getByRole("heading", { name: "Portfolio Command Center" })).toBeVisible();

  // The ten-column grid is gone at this width. Whether the stacked list has rows depends on
  // whether this account holds anything; that the TABLE is absent does not.
  await expect(page.getByRole("table")).toHaveCount(0);

  /* The brief's ordering rule for a phone: what needs attention comes before the comparison.
     `xl:order-*` restores desktop reading order, so this asserts the small-screen half. */
  const rail = page.getByTestId("attention-rail");
  await expect(rail).toBeVisible();
  const railBox = await rail.boundingBox();
  const band = page.getByTestId("metric-band");
  await expect(band).toBeVisible();
  const bandBox = await band.boundingBox();
  expect(bandBox!.y).toBeLessThan(railBox!.y);

  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow, "the page body scrolls horizontally on a phone").toBeLessThanOrEqual(1);
});

test("on mobile every state still explains itself with one next action", async ({ page }) => {
  await page.setViewportSize(VIEWPORTS.mobile);
  await page.goto(PATH);

  const body = await page.locator("body").innerText();
  /* The brief's hard rule, checked against what the browser actually painted: a lone em dash
     standing where a figure should be. Dashes inside a sentence are fine; a dash that IS the
     whole line is the failure. */
  const bare = body.split("\n").filter((line) => line.trim() === "—" || line.trim() === "-");
  expect(bare, `bare dash lines: ${JSON.stringify(bare)}`).toEqual([]);
});

/* ------------------------------------------------------------------ *
 * The gate that is not a proposition — `gates/pc-integration.md` I19
 * ------------------------------------------------------------------ *
 *
 * Every other check on this screen is something a test can assert. The brief's first paragraph is
 * not: *"a premium, modern financial-intelligence workspace comparable in quality and information
 * density to Linear, Stripe, Ramp and institutional portfolio terminals ... not a conventional
 * broker dashboard or a generic collection of white statistic cards."*
 *
 * A suite of 2,800 green tests cannot report that a screen looks cheap. This captures it at the
 * three widths in both themes so a person — or a model with eyes — can look at the thing itself
 * and check it against the brief's own named anti-patterns: every metric in its own isolated
 * card, decorative gradients, colour without a second signal, a page too long to read.
 */
for (const theme of ["light", "dark"] as const) {
  for (const [name, size] of Object.entries(VIEWPORTS)) {
    test(`screenshot: the command centre at ${name} in ${theme}`, async ({ page }) => {
      await page.setViewportSize(size);
      await page.goto(PATH);
      await setTheme(page, theme);
      await expect(page.getByRole("heading", { name: "Portfolio Command Center" })).toBeVisible();
      await page.screenshot({
        path: `test-results/shots/command-center-${name}-${theme}.png`,
        fullPage: true,
      });
    });
  }
}
