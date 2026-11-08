import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ActionableBadge } from "@/components/opportunities/swing-results";

describe("ActionableBadge", () => {
  it("shows Actionable for an actionable row on fresh data", () => {
    render(<ActionableBadge opportunity={{ actionable: true }} status="FRESH" />);
    expect(screen.getByTestId("actionable-badge")).toHaveTextContent("Actionable");
  });

  it("never shows Actionable on stale data", () => {
    render(<ActionableBadge opportunity={{ actionable: true }} status="STALE" />);
    expect(screen.getByTestId("actionable-badge")).toHaveTextContent("Not actionable — data stale/incomplete");
  });
});
