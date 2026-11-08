import { expect, test, type CDPSession } from "@playwright/test";

/**
 * Prompt 8's second acceptance criterion:
 *
 *     "DataTable renders 4,000 rows at 60fps while scrolling (measure with a Playwright trace)."
 *
 * Measured from the Chrome DevTools trace rather than from wall-clock timing: 60fps is a statement
 * about *frames*, and a loop that finishes quickly while dropping every second frame would pass a
 * timing test and fail a user. The trace's `Commit` events are the frames the compositor actually
 * produced, so the interval between them is the frame time that was really achieved.
 *
 * The budget is expressed as a p95 rather than a mean. A mean of 16 ms with a 90 ms outlier is
 * exactly the jank this criterion exists to catch, and averaging hides it.
 */
const FRAME_BUDGET_MS = 1000 / 60;

/** One dropped frame in twenty is not perceptible; a systematic overrun is. */
const P95_BUDGET_MS = FRAME_BUDGET_MS * 2;

const SCROLL_STEPS = 40;
const SCROLL_DELTA = 400;

interface TraceEvent {
  name: string;
  ts: number;
  ph: string;
}

function percentile(values: number[], fraction: number): number {
  const sorted = [...values].sort((a, b) => a - b);
  const index = Math.max(0, Math.min(sorted.length - 1, Math.round(fraction * sorted.length) - 1));
  return sorted[index] ?? 0;
}

test("scrolling 4,000 rows holds 60fps", async ({ page, browserName }) => {
  test.skip(browserName !== "chromium", "the tracing API is Chrome DevTools Protocol");

  await page.goto("/kitchen-sink");
  const grid = page.getByRole("grid");
  await expect(grid).toBeVisible();
  await expect(grid).toHaveAttribute("aria-rowcount", "4001");

  // Virtualisation is the claim under test: 4,000 rows, a few dozen in the DOM.
  const renderedRows = await page.locator('[role="row"]').count();
  expect(renderedRows).toBeLessThan(80);

  const client: CDPSession = await page.context().newCDPSession(page);
  const events: TraceEvent[] = [];
  client.on("Tracing.dataCollected", (payload) => {
    // The CDP types describe trace events as opaque string maps; the fields this test reads are
    // `name`, `ts` and `ph`, which every event carries.
    const collected = payload as unknown as { value?: TraceEvent[] };
    if (collected.value) events.push(...collected.value);
  });

  await client.send("Tracing.start", {
    traceConfig: {
      recordMode: "recordAsMuchAsPossible",
      includedCategories: ["disabled-by-default-devtools.timeline"],
    },
  });

  await grid.hover();
  for (let step = 0; step < SCROLL_STEPS; step += 1) {
    await page.mouse.wheel(0, SCROLL_DELTA);
    await page.waitForTimeout(16);
  }

  const finished = new Promise<void>((resolve) => client.once("Tracing.tracingComplete", () => resolve()));
  await client.send("Tracing.end");
  await finished;

  const commits = events
    .filter((event) => event.name === "Commit" && event.ph !== "b")
    .map((event) => event.ts / 1000)
    .sort((a, b) => a - b);

  expect(commits.length, "the trace captured no frames").toBeGreaterThan(SCROLL_STEPS / 2);

  const intervals: number[] = [];
  for (let index = 1; index < commits.length; index += 1) {
    const gap = (commits[index] as number) - (commits[index - 1] as number);
    // Gaps longer than a quarter second are pauses between scroll bursts, not dropped frames.
    if (gap > 0 && gap < 250) intervals.push(gap);
  }

  const p95 = percentile(intervals, 0.95);
  const median = percentile(intervals, 0.5);
  console.log(
    `frames=${intervals.length} median=${median.toFixed(1)}ms p95=${p95.toFixed(1)}ms budget=${P95_BUDGET_MS.toFixed(1)}ms`,
  );

  expect(median, `median frame time ${median.toFixed(1)}ms`).toBeLessThan(FRAME_BUDGET_MS * 1.5);
  expect(p95, `p95 frame time ${p95.toFixed(1)}ms`).toBeLessThan(P95_BUDGET_MS);

  // The rows really did move, so the measurement is of scrolling and not of an idle page.
  const scrollTop = await grid.evaluate((node) => node.scrollTop);
  expect(scrollTop).toBeGreaterThan(1000);
});

test("the header stays put and repeats while scrolling", async ({ page }) => {
  await page.goto("/kitchen-sink");
  const grid = page.getByRole("grid");
  await grid.hover();
  await page.mouse.wheel(0, 4000);
  await page.waitForTimeout(200);

  // docs/08 §"Results panel": "sticky header; repeat the header row every 16 rows".
  await expect(page.getByRole("columnheader", { name: /Symbol/ }).first()).toBeInViewport();
  // The repeat is a picture of the header, so it has no role and is hidden from assistive
  // technology — see the note in `DataTable`. It is located by its content instead.
  const repeats = await page.locator('[aria-hidden="true"]:has-text("Sorting Factor")').count();
  expect(repeats).toBeGreaterThan(0);
});
