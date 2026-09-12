import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";

import { MetricValue } from "@/components/portfolio/detail/primitives";
import { metric } from "@/lib/portfolio/command-center";

import { renderWorkspace } from "@/components/portfolio/detail/__tests__/render";

/** AFH 5.2 — missing figures are one line + tooltip, not a paragraph under every tile. */
describe("AFH 5.2 unavailable tiles", () => {
  it("shows Needs more data without the reason paragraph in the tile body", () => {
    const reason = "Cost basis is missing for this holding.";
    renderWorkspace(
      <MetricValue metric={metric("Cost", null, reason)} kind="rupees" />,
    );
    expect(screen.getByText("Needs more data")).toBeInTheDocument();
    expect(screen.queryByText(reason)).not.toBeInTheDocument();
    expect(screen.getByText("Needs more data").closest("[title]")?.getAttribute("title")).toBe(
      reason,
    );
  });
});
