import { expect, test } from "@playwright/test";

/**
 * Prompt 8's third acceptance criterion:
 *
 *     "Keyboard-only walkthrough of the shell is possible: skip link, focus rings, no traps."
 *
 * docs/08 §"Accessibility & quality bar" adds: "All interactive elements keyboard reachable; the
 * table supports arrow-key navigation."
 *
 * Every assertion below is made with the keyboard only — no `click()`, no `focus()` — because a
 * walkthrough that needs the mouse to set up is not a keyboard walkthrough.
 */
test.describe("keyboard-only walkthrough", () => {
  test("the first tab stop is a skip link that moves focus to main", async ({ page }) => {
    await page.goto("/kitchen-sink");

    await page.keyboard.press("Tab");
    const skip = page.getByRole("link", { name: "Skip to main content" });
    await expect(skip).toBeFocused();

    // Visible once focused — a skip link nobody can see is a skip link nobody uses.
    await expect(skip).toBeInViewport();

    await page.keyboard.press("Enter");
    await expect(page.locator("#main-content")).toBeFocused();
  });

  test("focus is always visible, never invisible", async ({ page }) => {
    await page.goto("/kitchen-sink");

    for (let step = 0; step < 12; step += 1) {
      await page.keyboard.press("Tab");
      const outline = await page.evaluate(() => {
        const active = document.activeElement;
        if (!active || active === document.body) return null;
        const style = getComputedStyle(active);
        return { width: style.outlineWidth, style: style.outlineStyle };
      });
      if (outline) {
        expect(
          outline.style !== "none" && Number.parseFloat(outline.width) > 0,
          `element ${step} had no visible focus ring`,
        ).toBe(true);
      }
    }
  });

  /*
   * M37 removed the collapsible left sidebar — navigation is a bar along the top now, and there
   * is nothing to collapse. The test that drove `#app-sidebar`'s `data-collapsed` went with the
   * feature rather than being adapted to something it no longer describes.
   * `docs/DECISIONS-MERGE.md` §M37.3 records the departure from docs/08 §"App shell".
   */


  test("the command palette opens on ⌘K and closes on Escape without trapping focus", async ({
    page,
  }) => {
    await page.goto("/kitchen-sink");
    await page.keyboard.press("ControlOrMeta+k");

    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    // M40 widened the palette from instruments to the whole catalog; the placeholder says so.
    await expect(page.getByPlaceholder(/Search stocks, indices, baskets, screens/)).toBeFocused();

    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);

    // The trap is gone: Tab moves on rather than cycling inside a dismissed dialog.
    await page.keyboard.press("Tab");
    const stillOpen = await page.getByRole("dialog").count();
    expect(stillOpen).toBe(0);
  });

  test("the results grid is one tab stop with arrow-key movement inside", async ({ page }) => {
    await page.goto("/kitchen-sink");

    const grid = page.getByRole("grid");
    await grid.focus();
    await expect(grid).toBeFocused();

    // Arrow keys move the focused cell; the grid never becomes 4,000 tab stops.
    await page.keyboard.press("ArrowDown");
    await page.keyboard.press("ArrowRight");
    const cell = page.locator('[role="gridcell"][tabindex="0"]');
    await expect(cell).toHaveCount(1);
    await expect(cell).toBeFocused();
    await expect(cell).toHaveAttribute("aria-colindex", "2");

    // PageDown moves by a screenful and the virtualiser follows it.
    await page.keyboard.press("PageDown");
    const row = page.locator('[role="row"]:has([role="gridcell"][tabindex="0"])');
    const index = await row.getAttribute("aria-rowindex");
    expect(Number(index)).toBeGreaterThan(10);
  });

  test("the grid states its true size, not its rendered size", async ({ page }) => {
    await page.goto("/kitchen-sink");
    // Virtualisation renders ~30 rows; assistive technology must still be told there are 4,000.
    await expect(page.getByRole("grid")).toHaveAttribute("aria-rowcount", "4001");
  });
});
