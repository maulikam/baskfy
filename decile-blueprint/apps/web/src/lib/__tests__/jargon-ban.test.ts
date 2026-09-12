import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { PAGES } from "@/lib/vocabulary";

const BANNED_IN_TITLES = ["Rebalance Tracker", "MomentumScan", "Time machine", "Stock finder"];

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) walk(p, out);
    else if (name === "page.tsx") out.push(p);
  }
  return out;
}

describe("consumer jargon ban (Tree 6)", () => {
  it("never uses banned strings as page metadata titles in PAGES", () => {
    for (const [path, entry] of Object.entries(PAGES)) {
      for (const banned of BANNED_IN_TITLES) {
        expect(entry.title, `${path} title`).not.toBe(banned);
      }
    }
  });

  it("no consumer basket surface carries desk/MomentumScan copy", () => {
    /*
     * This named one file — `src/app/(app)/discover/featured/page.tsx` — until `0716204` deleted
     * it as an orphan: nothing linked to it, and the Discover hub is the catalogue now. The page
     * went; the guarantee it was standing in for did not, so the sweep moved to the surfaces that
     * replaced it rather than following the deleted file into the bin.
     */
    const pages = [
      join(process.cwd(), "src/app/(app)/discover"),
      join(process.cwd(), "src/app/(app)/basket"),
    ].flatMap((root) => walk(root));
    expect(pages.length, "the sweep found no pages to read").toBeGreaterThanOrEqual(5);
    for (const file of pages) {
      const src = readFileSync(file, "utf8");
      expect(src, file).not.toMatch(/MomentumScan/);
      expect(src, file).not.toMatch(/for the desk/);
      expect(src, file).not.toMatch(/screen_run_id/);
      expect(src, file).not.toMatch(/data version/);
    }
  });

  it("no app page hardcodes Rebalance Tracker as metadata title", () => {
    const root = join(process.cwd(), "src/app");
    for (const file of walk(root)) {
      const src = readFileSync(file, "utf8");
      expect(src, file).not.toMatch(/title:\s*["']Rebalance Tracker["']/);
    }
  });

  it("backtest detail leaves the SEBI Disclaimer to AppShell", () => {
    // AppShell already mounts one per app page; a second on /backtests/[id] is a double-render.
    // The assumptions panel's run-honesty sentence is a different string and stays.
    const file = join(process.cwd(), "src/components/backtests/backtest-result.tsx");
    const src = readFileSync(file, "utf8");
    expect(src).not.toMatch(/import \{ Disclaimer \}/);
  });

  it("consumer chrome does not say data version to users", () => {
    for (const rel of [
      "src/components/shell/freshness-pill.tsx",
      "src/components/portfolios/buffer-explainer.tsx",
    ]) {
      const src = readFileSync(join(process.cwd(), rel), "utf8");
      expect(src, rel).not.toMatch(/data version/i);
    }
  });
});
