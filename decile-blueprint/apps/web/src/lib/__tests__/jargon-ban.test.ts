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

  it("featured basket page source has no desk/MomentumScan user-facing copy", () => {
    const file = join(
      process.cwd(),
      "src/app/(app)/baskets/featured/page.tsx",
    );
    const src = readFileSync(file, "utf8");
    expect(src).not.toMatch(/MomentumScan/);
    expect(src).not.toMatch(/for the desk/);
    expect(src).not.toMatch(/screen_run_id/);
    expect(src).not.toMatch(/data version/);
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
});
