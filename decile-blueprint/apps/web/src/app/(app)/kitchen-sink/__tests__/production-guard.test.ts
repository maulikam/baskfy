import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * AUDIT 1.23 — `/kitchen-sink` must 404 in production. The page calls `notFound()` when
 * `NODE_ENV === "production"`; this pins that guard in source so a remove-and-ship cannot slip.
 */
const PAGE = resolve(process.cwd(), "src/app/(app)/kitchen-sink/page.tsx");

describe("kitchen-sink production guard", () => {
  it("calls notFound when NODE_ENV is production", () => {
    const source = readFileSync(PAGE, "utf8");
    expect(source).toMatch(/NODE_ENV\s*===\s*["']production["']/);
    expect(source).toMatch(/notFound\s*\(\s*\)/);
  });
});
