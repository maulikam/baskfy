import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

describe("AFH 5.9 screen query pre-selects a backtest", () => {
  it("Build backtests page reads searchParams.screen", () => {
    const page = readFileSync(
      join(__dirname, "..", "..", "..", "app", "(app)", "build", "backtests", "page.tsx"),
      "utf8",
    );
    expect(page).toContain("searchParams");
    expect(page).toContain("params.screen");
    expect(page).toContain("initialScreenId");
  });

  it("ConfigForm accepts initialScreenId", () => {
    const form = readFileSync(join(__dirname, "..", "config-form.tsx"), "utf8");
    expect(form).toContain("initialScreenId");
  });
});
