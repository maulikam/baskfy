import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, resolve, sep } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * Prompt 13's fourth acceptance criterion, enforced rather than trusted:
 *
 *     "No price or entitlement is hard-coded in the web app; all read from the API."
 *
 * Same approach as `no-any.test.ts`: scan the source. A reviewer can miss a `₹500` added to a
 * component six months from now; a scan cannot. Every price on `/pricing` arrives from
 * `GET /plans`, and every entitlement from `GET /me` — so a literal that looks like one of
 * docs/01 §1's amounts, or a hand-written map of what a plan grants, is the bug this catches.
 */
const ROOTS = [resolve(process.cwd(), "src"), resolve(process.cwd(), "e2e")];

/**
 * Test files are excluded, and only test files.
 *
 * A fixture has to spell out a payload the API would send — `price_inr: "500.00"`, `max_screens:
 * 50` — or it is testing nothing. That is not a price hard-coded into the product: nothing in a
 * `__tests__` directory or a `*.test.tsx` ships. Everything that does ship is scanned.
 */
function isTestFile(path: string): boolean {
  return /\.(test|spec)\.tsx?$/.test(path) || path.includes(`${sep}__tests__${sep}`);
}

function sourceFiles(directory: string): string[] {
  return readdirSync(directory).flatMap((entry) => {
    const path = join(directory, entry);
    if (statSync(path).isDirectory()) return sourceFiles(path);
    return /\.tsx?$/.test(path) && !isTestFile(path) ? [path] : [];
  });
}

const FILES = ROOTS.flatMap(sourceFiles);

interface Offence {
  file: string;
  line: number;
  text: string;
}

function scan(pattern: RegExp): Offence[] {
  const found: Offence[] = [];
  for (const file of FILES) {
    readFileSync(file, "utf-8")
      .split("\n")
      .forEach((text, index) => {
        if (pattern.test(text)) {
          found.push({ file: relative(process.cwd(), file), line: index + 1, text: text.trim() });
        }
      });
  }
  return found;
}

function format(offences: Offence[]): string {
  return offences.map((o) => `${o.file}:${o.line}: ${o.text}`).join("\n");
}

describe("prices are never written into the web app", () => {
  /** docs/01 §1: "Monthly ₹500 · Yearly ₹3,999 · Forever ₹14,999 (rising to ₹899 / ₹5,999 / ₹19,999)". */
  const AMOUNTS = ["500", "3999", "3,999", "14999", "14,999", "899", "5999", "5,999", "19999", "19,999"];

  it.each(AMOUNTS)("does not contain the literal %s next to a rupee sign", (amount) => {
    const escaped = amount.replace(/,/g, ",");
    const offences = scan(new RegExp(`(₹|Rs\\.?|INR)\\s*${escaped}\\b`));
    expect(format(offences)).toBe("");
  });

  it("has no plan-price map", () => {
    const offences = scan(/price_inr\s*[:=]\s*["'`]?\d/);
    expect(format(offences)).toBe("");
  });

  it("never hard-codes a billing interval's price", () => {
    const offences = scan(/(monthly|yearly|forever)\s*[:=]\s*\d{3,}/i);
    expect(format(offences)).toBe("");
  });
});

describe("entitlements are never decided in the web app", () => {
  it("has no literal entitlement grant", () => {
    /**
     * `export_csv: true` in `apps/web` would be the UI deciding what a plan includes. docs/07
     * §Entitlements: "the UI only *reflects* entitlements."
     */
    const offences = scan(
      /\b(export_csv|custom_columns|historical_ranks|backtests|api_access)\s*:\s*(true|false)\b/,
    );
    expect(format(offences)).toBe("");
  });

  it("has no hard-coded max_screens", () => {
    const offences = scan(/max_screens\s*[:=]\s*\d/);
    expect(format(offences)).toBe("");
  });

  it("scanned a meaningful number of files", () => {
    // A scan over nothing would pass silently and prove nothing.
    expect(FILES.length).toBeGreaterThan(20);
  });
});
