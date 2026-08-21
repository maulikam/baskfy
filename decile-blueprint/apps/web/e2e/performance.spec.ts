import { writeFileSync, mkdirSync } from "node:fs";
import { join, resolve } from "node:path";

import { expect, test } from "@playwright/test";

/**
 * The two docs/11 §"Performance budgets" rows that only a browser can answer — Prompt 16
 * acceptance criterion 1, "every budget in docs/11 is met by an automated benchmark".
 *
 *     | Instrument factsheet (RSC) | TTFB < 300 ms, LCP < 1.8 s |
 *
 * TTFB is measured server-side too (`services/api/tests/test_benchmarks.py`), because the API
 * call is the floor under it. LCP has no server-side analogue at all: it is when the largest
 * element in the viewport finished painting, which depends on the HTML, the fonts, the JS and the
 * layout — nothing the API can tell you.
 *
 * Both numbers come from the browser's own Performance Timeline rather than from Lighthouse.
 * `lighthouse.spec.ts` next door runs the *accessibility* and *SEO* categories, which are static
 * audits and are stable in CI; the performance category simulates a slow network and a 4x-throttled
 * CPU, and its score on a shared GitHub runner moves by twenty points between runs. docs/11 states
 * a wall-clock budget, not a Lighthouse score, so the wall clock is what is measured.
 *
 * The factsheet is the page under test because docs/11 names it, and it is anonymous by design
 * (`docs/08` §Routes: "the organic-traffic surface"), so there is no sign-in in the measurement.
 */

const SYMBOL = "CUPID";

/** docs/11 §"Performance budgets". */
const LCP_BUDGET_MS = 1800;
const TTFB_BUDGET_MS = 300;

/**
 * How many loads to time. LCP on a cold Next server includes the first compile of the route, so
 * the first load is discarded and the median of the rest is reported — the same shape the
 * server-side benchmarks use.
 */
const SAMPLES = 5;

interface PageTimings {
  /** `responseStart - requestStart` on the navigation entry. */
  ttfbMs: number;
  /** The last `largest-contentful-paint` entry's `startTime`, in ms from navigation start. */
  lcpMs: number | null;
}

/** Repo root, for writing the measurement where `python -m benchmarks.report` will find it. */
const REPO_ROOT = resolve(process.cwd(), "..", "..");

function recordMeasurement(key: string, value: number, unit: string, method: string): void {
  const dir = join(REPO_ROOT, "benchmarks", "results");
  mkdirSync(dir, { recursive: true });
  writeFileSync(
    join(dir, `${key}.json`),
    `${JSON.stringify(
      { key, value, unit, method, dataset: "the e2e database (baskfy_e2e), production build" },
      null,
      2,
    )}\n`,
    "utf8",
  );
}

function median(values: number[]): number {
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 0 ? (sorted[middle - 1]! + sorted[middle]!) / 2 : sorted[middle]!;
}

test.describe("docs/11 performance budgets in a browser", () => {
  test(`the factsheet paints inside ${LCP_BUDGET_MS} ms and answers inside ${TTFB_BUDGET_MS} ms`, async ({
    page,
  }) => {
    test.slow();

    const lcpSamples: number[] = [];
    const ttfbSamples: number[] = [];

    for (let attempt = 0; attempt <= SAMPLES; attempt += 1) {
      await page.goto(`/instruments/${SYMBOL}`, { waitUntil: "load" });
      // The page has to have actually rendered for LCP to mean anything.
      await expect(page.getByRole("heading", { level: 1, name: SYMBOL })).toBeVisible();

      const timings: PageTimings = await page.evaluate(async () => {
        const navigation = performance.getEntriesByType("navigation")[0] as
          | PerformanceNavigationTiming
          | undefined;
        const ttfbMs = navigation ? navigation.responseStart - navigation.requestStart : 0;

        // `buffered: true` replays the entries the browser recorded before this observer existed,
        // which is every one of them — LCP is emitted during load, long before `evaluate` runs.
        const lcpMs = await new Promise<number | null>((resolve_) => {
          let latest: number | null = null;
          const observer = new PerformanceObserver((list) => {
            for (const entry of list.getEntries()) latest = entry.startTime;
          });
          try {
            observer.observe({ type: "largest-contentful-paint", buffered: true });
          } catch {
            // Not every engine implements the entry type; the assertion below skips rather than
            // asserting on a number nobody produced.
            resolve_(null);
            return;
          }
          // One frame plus a tick: enough for the buffered entries to be delivered.
          setTimeout(() => {
            observer.disconnect();
            resolve_(latest);
          }, 250);
        });

        return { ttfbMs, lcpMs };
      });

      // The first load pays for the route's first compile in the Next server; discard it.
      if (attempt === 0) continue;
      ttfbSamples.push(timings.ttfbMs);
      if (timings.lcpMs !== null) lcpSamples.push(timings.lcpMs);
    }

    expect(ttfbSamples.length, "no navigation timings were collected").toBeGreaterThan(0);
    const ttfb = median(ttfbSamples);
    expect(
      ttfb,
      `TTFB median ${ttfb.toFixed(0)} ms exceeds docs/11's ${TTFB_BUDGET_MS} ms ` +
        `(samples: ${ttfbSamples.map((s) => s.toFixed(0)).join(", ")})`,
    ).toBeLessThan(TTFB_BUDGET_MS);

    expect(
      lcpSamples.length,
      "the browser reported no largest-contentful-paint entry; the budget cannot be checked",
    ).toBeGreaterThan(0);
    const lcp = median(lcpSamples);
    expect(
      lcp,
      `LCP median ${lcp.toFixed(0)} ms exceeds docs/11's ${LCP_BUDGET_MS} ms ` +
        `(samples: ${lcpSamples.map((s) => s.toFixed(0)).join(", ")})`,
    ).toBeLessThan(LCP_BUDGET_MS);

    recordMeasurement(
      "factsheet_lcp",
      Number((lcp / 1000).toFixed(3)),
      "s",
      `median largest-contentful-paint over ${lcpSamples.length} loads of /instruments/${SYMBOL}`,
    );
  });
});
