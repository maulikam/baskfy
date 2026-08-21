import { expect, test, type CDPSession } from "@playwright/test";

/**
 * The three market-data surfaces end to end — Prompt 11 deliverables 2, 3 and 4.
 *
 * `dashboard renders every index and sorts client-side` is Prompt 11's second acceptance
 * criterion, and `listings pagination is stable` is a browser-side companion to the API's cursor
 * walk in `services/api/tests/test_api_market_data.py`.
 *
 * The seeded dataset (`decile_api.seed e2e`) carries 117 indices — docs/01 §7's "~145" minus the
 * names we would have had to invent; see `docs/11a` §1.
 */

const INDEX_COUNT = 117;

/** 60fps is 16.7ms; the same budget `table-performance.spec.ts` holds the results grid to. */
const FRAME_BUDGET_MS = 1000 / 60;
const P95_BUDGET_MS = 1000 / 30;
const SCROLL_STEPS = 24;
const SCROLL_DELTA = 240;

interface TraceEvent {
  name: string;
  ts: number;
  ph?: string;
}

function percentile(values: number[], fraction: number): number {
  const sorted = [...values].sort((a, b) => a - b);
  const index = Math.max(0, Math.min(sorted.length - 1, Math.round(fraction * sorted.length) - 1));
  return sorted[index] ?? 0;
}

test.describe("the indices dashboard", () => {
  test("renders every index, sorted by change descending", async ({ page }) => {
    await page.goto("/dashboard");

    const grid = page.getByRole("grid");
    await expect(grid).toBeVisible();
    // docs/01 §7's row count, stated by the grid rather than by what happens to be in the DOM.
    await expect(grid).toHaveAttribute("aria-rowcount", String(INDEX_COUNT + 1));
    await expect(page.getByText(`${INDEX_COUNT} of ${INDEX_COUNT} indices`, { exact: false })).toBeVisible();

    // Virtualised: 117 rows, a few dozen in the DOM.
    expect(await page.locator('[role="row"]').count()).toBeLessThan(80);
  });

  test("sorts client-side, issuing no request at all", async ({ page }) => {
    await page.goto("/dashboard");
    await expect(page.getByRole("grid")).toBeVisible();
    await page.waitForLoadState("networkidle");

    const requests: string[] = [];
    page.on("request", (request) => {
      if (request.resourceType() === "fetch" || request.resourceType() === "xhr") {
        requests.push(request.url());
      }
    });

    const firstBefore = await page.locator('[role="row"][aria-rowindex="2"]').textContent();
    await page.getByRole("columnheader", { name: /^PE/ }).click();
    await expect
      .poll(async () => page.locator('[role="row"][aria-rowindex="2"]').textContent())
      .not.toBe(firstBefore);

    await page.getByRole("columnheader", { name: /^Level/ }).click();
    await page.waitForTimeout(300);

    expect(requests, `sorting fetched: ${requests.join(", ")}`).toEqual([]);
  });

  test("scrolls without jank", async ({ page, browserName }) => {
    test.skip(browserName !== "chromium", "the tracing API is Chrome DevTools Protocol");
    test.slow();

    await page.goto("/dashboard");
    const grid = page.getByRole("grid");
    await expect(grid).toBeVisible();

    const client: CDPSession = await page.context().newCDPSession(page);
    const events: TraceEvent[] = [];
    client.on("Tracing.dataCollected", (payload) => {
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

    const finished = new Promise<void>((resolve) => {
      client.once("Tracing.tracingComplete", () => resolve());
    });
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
      if (gap > 0 && gap < 250) intervals.push(gap);
    }
    const median = percentile(intervals, 0.5);
    const p95 = percentile(intervals, 0.95);
    console.log(
      `dashboard frames=${intervals.length} median=${median.toFixed(1)}ms p95=${p95.toFixed(1)}ms`,
    );
    expect(median).toBeLessThan(FRAME_BUDGET_MS * 1.5);
    expect(p95).toBeLessThan(P95_BUDGET_MS);
  });

  test("searches and toggles between table and cards", async ({ page }) => {
    await page.goto("/dashboard");
    await page.getByLabel("Search indices").fill("bank");
    await expect
      .poll(async () => page.getByText(/of 117 indices/).textContent())
      .not.toContain(`${INDEX_COUNT} of`);

    await page.getByRole("button", { name: "Cards" }).click();
    await expect(page.getByRole("list", { name: "Indices" })).toBeVisible();
    await expect(page).toHaveURL(/view=cards/);
    await expect(page.getByRole("grid")).toHaveCount(0);
  });

  test("shows an em dash for an index with no fundamentals", async ({ page }) => {
    await page.goto("/dashboard?q=india+vix&view=cards");
    // Scoped to the card grid: the sidebar is a list of list items too.
    const card = page.getByRole("list", { name: "Indices" }).getByRole("listitem").first();
    await expect(card).toContainText("INDIA VIX");
    // docs/01 §7: "`India VIX` → PE/PB/DivYield shown as `-`".
    await expect(card.getByText("—")).toHaveCount(3);
  });
});

test.describe("market health", () => {
  test("renders the four gauges with docs/01 §6's wording", async ({ page }) => {
    await page.goto("/market-health");
    await expect(page.getByRole("heading", { level: 1 })).toContainText("Market Health — NIFTY 500");

    for (const label of [
      "Above 200 DMA",
      "Above 50 DMA",
      "Within 10% of ATH",
      "1Y Return > 0%",
    ]) {
      await expect(page.getByRole("figure").filter({ hasText: label }).first()).toBeVisible();
    }
  });

  test("states the date the data actually starts from", async ({ page }) => {
    await page.goto("/market-health");
    await expect(page.getByText(/Data available from /)).toBeVisible();
  });

  test("switching universe re-renders on the server and is shareable", async ({ page }) => {
    await page.goto("/market-health");
    await page.getByLabel("Universe").selectOption("nifty-microcap-250");
    await expect(page).toHaveURL(/universe=nifty-microcap-250/);
    await expect(page.getByRole("heading", { level: 1 })).toContainText("NIFTY MICROCAP 250");

    // The URL alone reproduces the view.
    await page.goto("/market-health?universe=nifty-50");
    await expect(page.getByRole("heading", { level: 1 })).toContainText("NIFTY 50");
  });

  test("offers docs/01 §6's twelve universes and no others", async ({ page }) => {
    await page.goto("/market-health");
    const options = page.getByLabel("Universe").locator("option");
    await expect(options).toHaveCount(12);
    await expect(options.filter({ hasText: "ETF" })).toHaveCount(0);
  });

  test("has a history chart per breadth series with a range picker", async ({ page }) => {
    await page.goto("/market-health");
    await expect(page.getByRole("heading", { name: "History" })).toBeVisible();
    await expect(page.getByRole("group", { name: "History range" })).toBeVisible();

    await page.getByRole("button", { name: "3M" }).click();
    await expect(page).toHaveURL(/range=3m/);
  });
});

test.describe("listings", () => {
  test("shows a page of the register, newest first", async ({ page }) => {
    await page.goto("/listings");
    await expect(page.getByRole("heading", { level: 1, name: "Listings" })).toBeVisible();
    await expect(page.getByRole("row")).toHaveCount(101); // 100 rows plus the header
  });

  test("pages forward on a cursor with no duplicates", async ({ page }) => {
    await page.goto("/listings");
    const firstPage = await page.locator("tbody th").allTextContents();

    await page.getByRole("link", { name: "Next page" }).click();
    await expect(page).toHaveURL(/cursor=/);
    const secondPage = await page.locator("tbody th").allTextContents();

    expect(secondPage.length).toBeGreaterThan(0);
    const overlap = secondPage.filter((symbol) => firstPage.includes(symbol));
    expect(overlap, `these symbols appeared on both pages: ${overlap.join(", ")}`).toEqual([]);

    // Back is the previous page, exactly, because each page is its own URL.
    await page.goBack();
    expect(await page.locator("tbody th").allTextContents()).toEqual(firstPage);
  });

  test("filters by series and searches", async ({ page }) => {
    await page.goto("/listings");
    // Whichever series is chosen, every row on the page must carry it — that is what the filter
    // claims. `EQ` is the one the seeded register is dominated by.
    await page.getByLabel("Series").selectOption("EQ");
    await expect(page).toHaveURL(/series=EQ/);
    const series = await page.locator("tbody tr td:nth-child(4)").allTextContents();
    expect(new Set(series)).toEqual(new Set(["EQ"]));

    await page.goto("/listings");
    await page.getByLabel("Search").fill("CUPID");
    await expect(page).toHaveURL(/search=CUPID/);
    await expect(page.locator("tbody th")).toHaveText(["CUPID"]);
  });

  test("links each symbol to its factsheet", async ({ page }) => {
    await page.goto("/listings?search=CUPID");
    await page.getByRole("link", { name: "CUPID" }).click();
    await expect(page).toHaveURL(/\/instruments\/CUPID$/);
  });
});

test.describe("the sitemap", () => {
  test("lists instrument URLs now that /listings can enumerate them", async ({ request }) => {
    // `docs/10a` §6 recorded this as blocked on Prompt 11's `/listings`. It is not any more.
    const response = await request.get("/sitemap.xml");
    expect(response.status()).toBe(200);
    const xml = await response.text();
    expect(xml).toContain("/instruments/CUPID");
    expect(xml).toContain("/market-health");
  });
});
