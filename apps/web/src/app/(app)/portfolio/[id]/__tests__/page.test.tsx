import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const loadPortfolioDetail = vi.fn();
const fetchInvestment = vi.fn();
const notFound = vi.fn(() => {
  throw new Error("NEXT_NOT_FOUND");
});

vi.mock("@/lib/portfolio/detail-fetch", () => ({
  loadPortfolioDetail: (...args: unknown[]) => loadPortfolioDetail(...args) as unknown,
  numericPortfolioId: (id: string) => (/^[0-9]+$/.test(id) ? Number(id) : null),
}));
vi.mock("@/lib/investments/fetch", () => ({
  fetchInvestment: (...args: unknown[]) => fetchInvestment(...args) as unknown,
}));
vi.mock("next/navigation", () => ({
  notFound: () => notFound(),
  usePathname: () => "/portfolio/6",
}));
vi.mock("@/components/portfolio/detail/workspace", () => ({
  PortfolioDetailWorkspace: () => <div data-testid="workspace" />,
}));
vi.mock("@/components/portfolio/detail/detail-slots", () => ({ RebalanceSlot: () => null }));
vi.mock("@/components/investments/drift-repair", () => ({ DriftRepair: () => null }));
vi.mock("@/components/investments/investment-actions", () => ({ InvestmentActions: () => null }));
vi.mock("@/components/investments/show-details-modal", () => ({ ShowDetailsModal: () => null }));
vi.mock("@/components/investments/sip-form", () => ({ SipForm: () => null }));
vi.mock("@/components/shell/section-tabs", () => ({ SectionTabs: () => null }));

import PortfolioDetailPage from "@/app/(app)/portfolio/[id]/page";

const failed = (missing: boolean) => ({
  detail: null,
  nav: null,
  activity: null,
  missing,
  failures: {
    detail: "This portfolio's summary did not load. The API did not answer in time — try again in a moment.",
    nav: null,
    activity: null,
  },
});

const params = (id: string) => ({ params: Promise.resolve({ id }) });

/** 15 Sep 2026: `/portfolio/6` read "Page not found" whenever its summary read timed out. */
describe("/portfolio/[id] when the summary does not load", () => {
  beforeEach(() => {
    loadPortfolioDetail.mockReset();
    fetchInvestment.mockReset();
    notFound.mockClear();
    fetchInvestment.mockResolvedValue(null);
  });

  it("says the summary did not load, with a retry, when the API did not answer", async () => {
    loadPortfolioDetail.mockResolvedValue(failed(false));
    render(await PortfolioDetailPage(params("6")));
    expect(notFound).not.toHaveBeenCalled();
    const status = screen.getByTestId("detail-unavailable");
    expect(status).toHaveTextContent("did not load");
    expect(screen.getByRole("link", { name: "Try again" })).toHaveAttribute("href", "/portfolio/6");
  });

  it("is not a page only when the API answered 404 and no investment has the id", async () => {
    loadPortfolioDetail.mockResolvedValue(failed(true));
    await expect(PortfolioDetailPage(params("6"))).rejects.toThrow("NEXT_NOT_FOUND");
    expect(notFound).toHaveBeenCalledTimes(1);
  });

  it("renders the workspace when the summary loads", async () => {
    loadPortfolioDetail.mockResolvedValue({
      ...failed(false),
      detail: { summary: { portfolio_id: 6, name: "Swing Manual" } },
      failures: { detail: null, nav: null, activity: null },
    });
    render(await PortfolioDetailPage(params("6")));
    expect(screen.getByTestId("workspace")).toBeInTheDocument();
  });
});
