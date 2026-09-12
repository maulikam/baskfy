import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { HeadlineStrip } from "@/components/market/headline-strip";
import type { IndexRowOut } from "@baskfy/api-client";

function row(slug: string, name: string, change_pct: string): IndexRowOut {
  return {
    slug,
    name,
    is_universe: true,
    level: "1000",
    change_pct,
    change_abs: "10",
  };
}

describe("AFH 5.6 headline strip", () => {
  it("pins NIFTY 50, Bank, Midcap 150, Smallcap 250 and VIX above the table", () => {
    render(
      <HeadlineStrip
        rows={[
          row("nifty-50", "NIFTY 50", "1.2"),
          row("nifty-bank", "NIFTY BANK", "-0.4"),
          row("nifty-midcap-150", "NIFTY MIDCAP 150", "0.8"),
          row("nifty-smallcap-250", "NIFTY SMALLCAP 250", "0.1"),
          row("india-vix", "India VIX", "2.0"),
          row("nifty-500", "NIFTY 500", "0.5"),
        ]}
      />,
    );
    expect(screen.getByTestId("market-headline-strip")).toBeInTheDocument();
    expect(screen.getByTestId("headline-nifty-50")).toHaveTextContent("NIFTY 50");
    expect(screen.getByTestId("headline-nifty-bank")).toHaveTextContent("Bank Nifty");
    expect(screen.getByTestId("headline-nifty-midcap-150")).toHaveTextContent("Midcap 150");
    expect(screen.getByTestId("headline-nifty-smallcap-250")).toHaveTextContent("Smallcap 250");
    expect(screen.getByTestId("headline-india-vix")).toHaveTextContent("India VIX");
  });
});
