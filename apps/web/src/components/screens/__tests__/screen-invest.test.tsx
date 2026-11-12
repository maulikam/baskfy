import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ScreenInvestActions } from "@/components/screens/screen-invest";

const state = vi.hoisted((): { data: unknown; isError: boolean } => ({ data: undefined, isError: false }));
vi.mock("@/lib/portfolios/queries", () => ({
  usePortfolios: () => ({ data: state.data, isError: state.isError, refetch: vi.fn() }),
}));

function show() {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <ScreenInvestActions screenName="AVG SHARP 12-1M" screenPublicId="d3d692da645c" />
    </QueryClientProvider>,
  );
}

const node = (id: number, screen_public_id: string | null) =>
  ({ id, name: "AVG SHARP 12-1M", screen_public_id, children: [] }) as unknown;

describe("investing from a screen never duplicates a portfolio by accident (12 Nov 2026)", () => {
  it("offers no Invest while the portfolio list is still loading", () => {
    state.data = undefined;
    state.isError = false;
    show();
    expect(screen.queryByTestId("screen-invest")).toBeNull();
    expect(screen.getByTestId("screen-invest-checking")).toBeDisabled();
  });

  it("offers a retry, not Invest, when the list fails to load", () => {
    state.data = undefined;
    state.isError = true;
    show();
    expect(screen.queryByTestId("screen-invest")).toBeNull();
    expect(screen.getByTestId("screen-invest-retry")).toBeInTheDocument();
  });

  it("offers Rebalance once a portfolio from this screen exists", () => {
    state.data = { data: [node(11, "d3d692da645c")], orphans: [] };
    state.isError = false;
    show();
    expect(screen.getByTestId("screen-rebalance")).toBeInTheDocument();
    expect(screen.queryByTestId("screen-invest")).toBeNull();
  });

  it("offers Invest only when the loaded list has none from this screen", () => {
    state.data = { data: [node(9, null)], orphans: [] };
    state.isError = false;
    show();
    expect(screen.getByTestId("screen-invest")).toBeInTheDocument();
  });
});
