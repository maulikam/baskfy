import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * AUDIT 4.7 — inert page `revalidate` removed; market fetch carries AbortSignal.timeout.
 */
describe("inert revalidate removed; market fetch times out", () => {
  for (const relative of [
    "app/(app)/market/today/page.tsx",
    "app/(app)/market/mood/page.tsx",
    "app/(app)/instruments/[symbol]/page.tsx",
  ] as const) {
    it(`${relative} has no export const revalidate`, () => {
      const source = readFileSync(resolve(process.cwd(), "src", relative), "utf8");
      expect(source).not.toMatch(/export const revalidate\s*=/);
    });
  }

  it("lib/market/fetch.ts uses AbortSignal.timeout", () => {
    const source = readFileSync(resolve(process.cwd(), "src/lib/market/fetch.ts"), "utf8");
    expect(source).toMatch(/AbortSignal\.timeout/);
  });
});
