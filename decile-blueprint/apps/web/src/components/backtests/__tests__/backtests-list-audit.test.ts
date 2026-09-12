import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * AUDIT 4.3 — delete must confirm, surface errors, and not clip the table on a phone.
 */
const SOURCE = readFileSync(
  resolve(process.cwd(), "src/components/backtests/backtests-list.tsx"),
  "utf8",
);

describe("backtests-list delete and overflow", () => {
  it("confirms before delete instead of void mutateAsync alone", () => {
    expect(SOURCE).toMatch(/window\.confirm/);
    expect(SOURCE).not.toMatch(/onClick=\{\(\) => void remove\.mutateAsync/);
  });

  it("renders remove.isError", () => {
    expect(SOURCE).toMatch(/remove\.isError/);
  });

  it("uses overflow-x-auto rather than overflow-hidden on the table wrapper", () => {
    expect(SOURCE).toMatch(/overflow-x-auto/);
    expect(SOURCE).not.toMatch(/overflow-hidden rounded-lg border border-border/);
  });

  it("links to /build/backtests/:id without as never on a legacy /backtests path", () => {
    expect(SOURCE).toMatch(/\/build\/backtests\/\$\{/);
    expect(SOURCE).not.toMatch(/`\/backtests\/\$\{/);
  });
});
