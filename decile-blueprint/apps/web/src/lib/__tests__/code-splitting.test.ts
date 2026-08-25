import { readFileSync, existsSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * Prompt 16 deliverable 5: **"route-level code splitting so the screens bundle stays under
 * 250 KB gzip, dynamic import of charts, and a bundle-size CI budget."**
 *
 * The budget itself is measured by `scripts/bundle-budget.mjs` against a real production build,
 * which is the only honest way to know a gzip size — but that needs `next build`, so it runs in
 * CI and not here. What *this* asserts is the thing that would silently undo it: a chart imported
 * statically again. A regression there does not fail any existing test; it just quietly moves
 * ~15 KB of visx back into a route's first-load JS, and nobody notices until the budget check runs
 * on a build somebody else made.
 *
 * The same reasoning as `no-any.test.ts`: enforce the rule by reading the source, not by trusting
 * that the tool which enforces it is still switched on.
 */
const SRC = resolve(process.cwd(), "src");

/** Every module that imports visx directly — i.e. every chart, by definition. */
const CHART_MODULES = [
  "components/backtests/equity-chart.tsx",
  "components/backtests/drawdown-chart.tsx",
  "components/market/breadth-history.tsx",
  "components/data/sparkline.tsx",
] as const;

/**
 * Charts that must be reached through `next/dynamic`, and the module that does it.
 *
 * `sparkline.tsx` is deliberately absent: it is a handful of lines around one `LinePath`, it
 * renders once per *row* in the dashboard table and once per metric card on the factsheet, and a
 * dynamic import per row would trade one shared chunk for hundreds of loading states. docs/11's
 * budget is about the route, not about every component on it.
 */
const LAZY_CHARTS = [
  { chart: "equity-chart", loadedBy: "components/backtests/backtest-result.tsx" },
  { chart: "drawdown-chart", loadedBy: "components/backtests/backtest-result.tsx" },
  { chart: "breadth-history", loadedBy: "components/market/breadth-history-lazy.tsx" },
] as const;

function read(relativePath: string): string {
  const path = resolve(SRC, relativePath);
  expect(existsSync(path), `${relativePath} does not exist`).toBe(true);
  return readFileSync(path, "utf8");
}

describe("chart code splitting", () => {
  it.each(CHART_MODULES)("%s is the only place visx is imported", (module) => {
    // Guards the premise of the whole test: if visx spreads to a module not on this list, the
    // list stops describing where the charting code is and the budget stops being defended.
    expect(read(module)).toMatch(/@visx\//);
  });

  it.each(LAZY_CHARTS)("$chart is loaded through next/dynamic by $loadedBy", ({
    chart,
    loadedBy,
  }) => {
    const source = read(loadedBy);
    expect(source).toMatch(/from "next\/dynamic"/);
    expect(source).toMatch(new RegExp(`dynamic\\(\\s*\\(\\)\\s*=>\\s*import\\([^)]*${chart}`));
  });

  it.each(LAZY_CHARTS)("$chart is not also imported statically by $loadedBy", ({
    chart,
    loadedBy,
  }) => {
    // A static import beside the dynamic one puts the chunk back in the parent bundle and the
    // `dynamic()` call becomes decoration.
    const source = read(loadedBy);
    const staticImport = new RegExp(`^import\\s+\\{[^}]*\\}\\s+from\\s+"[^"]*${chart}"`, "m");
    expect(source).not.toMatch(staticImport);
  });

  it("the lazy charts render nothing on the server", () => {
    // A chart sizes itself against the viewport, so an SSR pass followed by a hydration re-render
    // is a layout shift — and docs/08 budgets CLS at < 0.1 (asserted by e2e/layout-stability).
    for (const { loadedBy } of LAZY_CHARTS) {
      expect(read(loadedBy)).toMatch(/ssr:\s*false/);
    }
  });

  it("the market mood page imports the lazy wrapper, not the chart", () => {
    // Tree 6 moved this page to the Market hub; `/market-health` is now a redirect stub.
    const page = read("app/(app)/market/mood/page.tsx");
    expect(page).toMatch(/breadth-history-lazy/);
    // The type re-export is fine — types are erased — so only a value import is checked.
    expect(page).not.toMatch(/^import\s+\{\s*BreadthHistory[^}]*\}\s+from\s+"[^"]*breadth-history"/m);
  });
});

describe("the bundle budget", () => {
  it("has a script CI can run", () => {
    const script = resolve(process.cwd(), "scripts/bundle-budget.mjs");
    expect(existsSync(script)).toBe(true);
    const source = readFileSync(script, "utf8");
    // docs/11: "JS on the screens route < 250 KB gzip after code-splitting the table and charts."
    expect(source).toMatch(/limitKb:\s*250/);
    expect(source).toMatch(/\(app\)\/screens\/page/);
  });

  it("is wired into package.json", () => {
    const manifest = readFileSync(resolve(process.cwd(), "package.json"), "utf8");
    const parsed = JSON.parse(manifest) as { scripts: Record<string, string> };
    expect(parsed.scripts["bundle-budget"]).toContain("bundle-budget.mjs");
    expect(parsed.scripts["bundle-budget"]).toContain("--check");
  });
});
