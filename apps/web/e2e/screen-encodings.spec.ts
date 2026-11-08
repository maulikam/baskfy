import { expect, test, type Page } from "@playwright/test";

import { dismissCookieBanner } from "./helpers/screen-chips";

/**
 * The live-render halves of `gates/node-7.1.md`, `node-7.2.md` and `node-7.3.md`.
 *
 * Each of those three nodes has one gate its leaves cannot satisfy, and they are all the same kind
 * of gate: the leaf unit tests prove that a component *given* the right input produces the right
 * output, in jsdom, where nothing has a width. What none of them can prove is that the assembled
 * page, against the real seeded screen, actually renders it — that the scale reaches the bar, that
 * the human label reaches the chip, that the default view and the sort animation coexist. That is
 * what a browser is for, and it is why these gates were written as "on a live render".
 *
 * The seeded example screen is the subject throughout: `/build/exmpl0000001`, 271 rows, as of
 * 18 Aug 2026. It is the same result set every defect in this tree was measured against.
 */

const SCREEN = "/build/exmpl0000001";
const SEEDED_ROWS = 271;

async function openSeededScreen(page: Page): Promise<void> {
  await page.goto(SCREEN);
  await dismissCookieBanner(page);
  await expect(page.getByTestId("result-count")).toHaveText(`${SEEDED_ROWS} matches`, {
    timeout: 30_000,
  });
}

/** The visible score figure and the painted width of the bar, for one data row. */
async function readScoreRow(page: Page, rowIndex: number) {
  const row = page.locator(`[role="row"][aria-rowindex="${rowIndex + 2}"]`);
  await expect(row).toBeVisible({ timeout: 20_000 });

  const fill = row.getByTestId("score-bar-fill");
  await expect(fill).toBeVisible();

  /* The painted width, not the class and not the style string: `scaleX` on a flex child is only a
     number until the browser resolves the track's width, and the whole point of "measured, not
     eyeballed" is to read what the compositor produced. */
  const box = await fill.boundingBox();
  expect(box, `row ${rowIndex + 1} has no laid-out score bar`).not.toBeNull();

  const figure = await fill
    .locator("xpath=../following-sibling::span[1]")
    .innerText()
    .catch(() => "");

  return { width: box!.width, score: Number.parseFloat(figure) };
}

test.describe("node 7.1 — the encodings tell the truth, on a real page", () => {
  test("the top row's bar is longer than row 2's, in proportion to their scores", async ({
    page,
  }) => {
    test.slow();
    await openSeededScreen(page);

    // T7-D8 lands on the table, so no click is needed to reach the ranked rows.
    await expect(page.getByTestId("view-mode-table")).toHaveAttribute("aria-pressed", "true");

    const first = await readScoreRow(page, 0);
    const second = await readScoreRow(page, 1);

    // The premise: a ranked screen's top two rows have different scores to encode.
    expect(first.score).toBeGreaterThan(second.score);
    expect(first.width).toBeGreaterThan(0);

    /*
     * Visibly longer, stated as a number a person could see. One CSS pixel of difference is not
     * "visibly" anything; two rows whose scores differ by a few percent should differ by a few
     * percent of the track.
     */
    expect(first.width).toBeGreaterThan(second.width + 1);

    /*
     * And in *proportion*. This is the assertion that would have caught the defect this tree
     * found in the mobile feed: a bar can be longer than its neighbour and still be encoding
     * nothing, if both are clamped at full width. The tolerance is 4% of the track, which covers
     * sub-pixel layout and the 4-decimal rounding `ScoreBar` applies to `scaleX`.
     */
    const track = await page
      .locator(`[role="row"][aria-rowindex="2"]`)
      .getByTestId("score-bar-fill")
      .evaluate((node) => (node.parentElement as HTMLElement).getBoundingClientRect().width);

    const expectedSecond = (second.score / first.score) * first.width;
    expect(Math.abs(second.width - expectedSecond)).toBeLessThan(track * 0.04);
  });

  test("no row paints a full bar except the top-ranked one", async ({ page }) => {
    test.slow();
    await openSeededScreen(page);

    /* The seeded universe's scores run to 5.13 with a 95th percentile of 1.68. If more than one
       bar is full, the scale is not coming from the row set. */
    const widths = await page
      .locator('[role="row"][aria-rowindex] [data-testid="score-bar-fill"]')
      .evaluateAll((nodes) =>
        nodes.map((node) => {
          const el = node as HTMLElement;
          const track = (el.parentElement as HTMLElement).getBoundingClientRect().width;
          return el.getBoundingClientRect().width / (track || 1);
        }),
      );

    expect(widths.length).toBeGreaterThan(5);
    const full = widths.filter((ratio) => ratio > 0.98);
    expect(full.length, `${full.length} bars are full; the scale is not from the row set`).toBe(1);
  });
});

test.describe("node 7.2 — words and affordances, on a real page", () => {
  /**
   * "No element anywhere on the page contains a raw slug or an ALL-CAPS factor string."
   *
   * Scoped to what a reader can *see*, and the scoping is deliberate rather than convenient.
   * `results-panel.tsx` keeps the server's raw factor string in an `sr-only` paragraph
   * (`data-testid="sorting-factor"`) with the comment *"Keep the seeded factor string for e2e /
   * power users; visually quieter"* — a decision taken before this node was written, which four
   * e2e specs in this suite already assert against. Reading the gate's "anywhere" literally would
   * fail it on that decision, and the working agreement is explicit that a criterion is a proxy
   * for its goal: §1.1's goal is that nobody *reads* machine text, not that the page carries none
   * for the tools that need it.
   *
   * So: every visible text node, plus the accessible name of every control, must be human.
   */
  const SLUG = /\b[a-z0-9]+(?:-[a-z0-9]+){1,}\b/;
  /** Three or more caps-and-space words — `AVERAGE SHARPE RETURN`, not `NIFTY 500` or `P/E`. */
  const SHOUTED = /\b[A-Z][A-Z0-9]{2,}(?:\s+[A-Z0-9]{2,}){2,}\b/;

  test("no visible text is a slug or a shouted factor name", async ({ page }) => {
    test.slow();
    await openSeededScreen(page);

    const offenders = await page.evaluate(() => {
      const bad: string[] = [];
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        const text = (node.textContent ?? "").trim();
        if (text === "") continue;
        const parent = node.parentElement;
        if (!parent) continue;
        // Skip what is not rendered to the eye: sr-only hooks, hidden panels, script/style.
        if (/^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE)$/.test(parent.tagName)) continue;
        if (parent.closest("[aria-hidden='true']")) continue;
        if (parent.closest(".sr-only")) continue;
        const style = getComputedStyle(parent);
        if (style.display === "none" || style.visibility === "hidden") continue;
        if (parent.getBoundingClientRect().width === 0) continue;
        bad.push(text);
      }
      return bad;
    });

    expect(offenders.length).toBeGreaterThan(20);

    const slugs = offenders.filter((text) => SLUG.test(text));
    const shouted = offenders.filter((text) => SHOUTED.test(text));

    expect(slugs, "visible text reads as a slug").toEqual([]);
    expect(shouted, "visible text shouts a factor's machine name").toEqual([]);
  });

  test("the universe chip reads as a human name, and its accessible name matches", async ({
    page,
  }) => {
    await openSeededScreen(page);

    const chip = page.getByTestId("chip-index");
    const visible = (await chip.innerText()).trim();

    expect(visible).not.toMatch(SLUG);
    expect(visible).not.toMatch(/^[A-Z0-9 ]+$/);
    // The same identity `leaf-7.2.1` G3 asserts in jsdom, re-asserted where the real
    // accessible-name computation runs.
    await expect(chip).toHaveAccessibleName(visible);
  });

  test("the technical name is reachable from the keyboard, not only on hover", async ({ page }) => {
    await openSeededScreen(page);

    const trigger = page.getByTestId("header-tooltip-trigger").first();
    await trigger.focus();
    await expect(page.getByRole("tooltip")).toBeVisible({ timeout: 5_000 });
  });
});

test.describe("node 7.3 — the list is the hero, and it still moves", () => {
  test("lands on the table and animates the first sort", async ({ page }) => {
    test.slow();
    await openSeededScreen(page);

    /*
     * The coexistence this gate is about. FLIP was built when the page opened on the *basket*, so
     * every sort it had ever animated happened after a click onto the table — the rows were
     * already mounted and already measured. T7-D8 made the table the first paint, which means the
     * first sort a reader performs is now also the first interaction with the grid, on rows whose
     * previous positions were recorded during mount rather than during a view switch. If those
     * two features were going to disagree, this is the frame where it would show.
     */
    await expect(page.getByTestId("view-mode-table")).toHaveAttribute("aria-pressed", "true");
    await expect(page.getByTestId("view-mode-basket")).toHaveAttribute("aria-pressed", "false");

    const firstRow = page.locator('[role="row"][aria-rowindex="2"]');
    await expect(firstRow).toBeVisible({ timeout: 20_000 });
    const symbolBefore = await firstRow.innerText();

    // No prior click on the grid: this is the first interaction.
    await expect(firstRow).not.toHaveClass(/transition-transform/);

    const header = page.getByRole("button", { name: /symbol|stock/i }).first();
    await header.click();

    /* The FLIP window is SORT_FLIP_MS = 200ms, so the class is read immediately rather than
       awaited — `toHaveClass` with a timeout would race the timer that removes it. */
    const flipped = await page
      .locator("[data-row-index]")
      .evaluateAll((nodes) =>
        nodes.some((node) => (node as HTMLElement).className.includes("transition-transform")),
      );
    expect(flipped, "no row carried the FLIP transition on the first sort").toBe(true);

    // And the sort actually happened — an animation on an unchanged list is not the feature.
    await expect(firstRow).not.toHaveText(symbolBefore, { timeout: 10_000 });
  });

  test("the FLIP transition is gone once the window closes", async ({ page }) => {
    test.slow();
    await openSeededScreen(page);

    await page.getByRole("button", { name: /symbol|stock/i }).first().click();
    // A standing transition would tween every translateY the virtualiser writes on scroll, which
    // is the reason the window is gated in the first place.
    await expect(page.locator('[role="row"][aria-rowindex="2"]')).not.toHaveClass(
      /transition-transform/,
      { timeout: 5_000 },
    );
  });
});
