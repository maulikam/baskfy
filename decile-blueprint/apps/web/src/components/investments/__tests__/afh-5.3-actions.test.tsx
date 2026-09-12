import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

const ACTIONS = join(__dirname, "..", "investment-actions.tsx");
const MODAL = join(__dirname, "..", "..", "cb", "market-closed-modal.tsx");

/** AFH 5.3 — hollow primary CTAs demoted; Notify me gone; market-closed inline. */
describe("AFH 5.3 invest actions", () => {
  it("renders Invest more / Exit / Rebalance as outline (secondary) buttons", () => {
    const source = readFileSync(ACTIONS, "utf8");
    expect(source).toContain('variant="outline"');
    expect(source).not.toMatch(/variant=\{kind === "exit" \? "outline" : "primary"\}/);
    expect(source).not.toContain("If market closed");
    expect(source).toContain("market-closed-inline");
  });

  it("deletes Notify me from the market-closed modal", () => {
    const source = readFileSync(MODAL, "utf8");
    expect(source).not.toContain("Notify me");
  });
});
