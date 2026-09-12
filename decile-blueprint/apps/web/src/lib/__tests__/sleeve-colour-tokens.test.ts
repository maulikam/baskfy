import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, resolve } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * AUDIT 4.9 — sleeve status colours use design tokens, not raw Tailwind palette classes.
 */
const ROOTS = [
  resolve(process.cwd(), "src/app/(app)/swing"),
  resolve(process.cwd(), "src/app/(app)/vbt"),
  resolve(process.cwd(), "src/app/(app)/twt"),
];

const FORBIDDEN =
  /\b(?:text|bg|border)-(?:red|amber|emerald|green)-(?:50|100|200|300|400|500|600|700|800|900)\b/;

function walk(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) out.push(...walk(path));
    else if (path.endsWith(".tsx") || path.endsWith(".ts")) out.push(path);
  }
  return out;
}

describe("sleeve status colours use tokens", () => {
  it("has no raw red/amber/emerald palette classes under swing/vbt/twt pages", () => {
    const hits: string[] = [];
    for (const root of ROOTS) {
      for (const file of walk(root)) {
        const source = readFileSync(file, "utf8");
        if (FORBIDDEN.test(source)) hits.push(file.replace(process.cwd() + "/", ""));
      }
    }
    expect(hits).toEqual([]);
  });
});
