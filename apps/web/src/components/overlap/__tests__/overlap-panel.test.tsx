import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { OverlapPanel } from "@/components/overlap/overlap-panel";
import { intersectionOf } from "@/lib/overlap/overlap";

describe("OverlapPanel", () => {
  it("lists the shared symbols when both sources answered", () => {
    const result = intersectionOf([
      { key: "vbt", label: "Volume breakout", symbols: ["TCS", "INFY", "WIPRO"] },
      { key: "twt", label: "Three weeks tight", symbols: ["INFY", "TCS"] },
    ]);
    render(
      <OverlapPanel
        testId="overlap-pair"
        title="Volume breakout ∩ Three weeks tight"
        blurb="Names on both."
        result={result}
      />,
    );
    expect(screen.getByTestId("overlap-pair-summary").textContent).toMatch(/share 2 stocks/);
    expect(screen.getByTestId("overlap-pair-list").textContent).toMatch(/INFY/);
    expect(screen.getByTestId("overlap-pair-list").textContent).toMatch(/TCS/);
  });

  it("does not show a zero list when a source could not be read", () => {
    const result = intersectionOf([
      { key: "vbt", label: "Volume breakout", symbols: ["TCS"] },
      { key: "twt", label: "Three weeks tight", symbols: null },
    ]);
    render(
      <OverlapPanel
        testId="overlap-pair"
        title="Volume breakout ∩ Three weeks tight"
        blurb="Names on both."
        result={result}
      />,
    );
    expect(screen.getByTestId("overlap-pair-summary")).toHaveAttribute(
      "data-available",
      "false",
    );
    expect(screen.queryByTestId("overlap-pair-list")).toBeNull();
    expect(screen.queryByTestId("overlap-pair-empty")).toBeNull();
  });
});
