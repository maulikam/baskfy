import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ACTIVITY_EMPTY_CONNECTED } from "@/components/portfolio/activity-honest-copy";

import PortfolioActivityPage from "../page";

/**
 * The empty state used to claim the ledger had not been built, and to tell a connected account
 * that syncing would never help. `GET /portfolio/activity` is the feed. Holdings sync still does
 * not write trades, so an empty list after a sync is a real empty list.
 */
vi.mock("@/lib/brokers/fetch", () => ({
  fetchBrokerCatalog: vi.fn(),
}));

vi.mock("@/lib/portfolio/fetch", () => ({
  fetchPortfolioOverview: vi.fn(),
  fetchPortfolioActivity: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  usePathname: () => "/portfolio/activity",
}));

const { fetchBrokerCatalog } = await import("@/lib/brokers/fetch");
const { fetchPortfolioOverview, fetchPortfolioActivity } = await import("@/lib/portfolio/fetch");
const mockedCatalog = vi.mocked(fetchBrokerCatalog);
const mockedOverview = vi.mocked(fetchPortfolioOverview);
const mockedActivity = vi.mocked(fetchPortfolioActivity);

const catalog = (connected: boolean) =>
  ({ brokers: [{ id: "zerodha", connected }] }) as unknown as Awaited<
    ReturnType<typeof fetchBrokerCatalog>
  >;

const overview = (syncSummary: string) =>
  ({
    sync_summary: syncSummary,
    live_overlay: false,
  }) as Awaited<ReturnType<typeof fetchPortfolioOverview>>;

describe("the Activity page", () => {
  it("does not tell a connected account to connect", async () => {
    mockedCatalog.mockResolvedValue(catalog(true));
    mockedOverview.mockResolvedValue(overview("Holdings not synced yet"));
    mockedActivity.mockResolvedValue({ items: [], total: 0 });
    render(await PortfolioActivityPage());
    expect(screen.queryByRole("link", { name: "Connect a broker" })).not.toBeInTheDocument();
    expect(screen.getByTestId("sync-summary")).toHaveTextContent("Holdings not synced yet");
  });

  it("does not invent synced from a connected broker alone", async () => {
    mockedCatalog.mockResolvedValue(catalog(true));
    mockedOverview.mockResolvedValue(overview("primary has never synced"));
    mockedActivity.mockResolvedValue({ items: [], total: 0 });
    render(await PortfolioActivityPage());
    expect(screen.getByTestId("sync-summary")).toHaveTextContent("primary has never synced");
    expect(screen.getByTestId("activity-empty")).not.toHaveTextContent("holdings are synced");
  });

  it("says holdings sync will not invent trades, without claiming the feed is unbuilt", async () => {
    mockedCatalog.mockResolvedValue(catalog(true));
    mockedOverview.mockResolvedValue(overview("Holdings synced: 2026-09-11"));
    mockedActivity.mockResolvedValue({ items: [], total: 0 });
    render(await PortfolioActivityPage());
    expect(screen.getByTestId("activity-empty")).toHaveTextContent(ACTIVITY_EMPTY_CONNECTED);
    expect(screen.getByTestId("activity-empty")).not.toHaveTextContent("ledger behind it is built");
  });

  it("lists the feed the API returned, newest first as served", async () => {
    mockedCatalog.mockResolvedValue(catalog(true));
    mockedOverview.mockResolvedValue(overview("Holdings synced: 2026-09-11"));
    mockedActivity.mockResolvedValue({
      items: [
        {
          kind: "DIVIDEND",
          on: "2026-07-14",
          description: "Dividend from TCS",
          amount: "2400.00",
          is_pnl_event: true,
        },
        {
          kind: "CORPORATE_ACTION",
          on: "2026-06-02",
          description: "Bonus on INFY",
          is_pnl_event: false,
        },
      ],
      total: 2,
    });
    render(await PortfolioActivityPage());
    expect(screen.getByTestId("activity-list")).toBeInTheDocument();
    expect(screen.getByText("Dividend from TCS")).toBeInTheDocument();
    expect(screen.getByText("Bonus on INFY")).toBeInTheDocument();
    expect(screen.queryByTestId("activity-empty")).not.toBeInTheDocument();
  });

  it("still offers Connect when nothing is connected", async () => {
    mockedCatalog.mockResolvedValue(catalog(false));
    mockedOverview.mockResolvedValue(null);
    mockedActivity.mockResolvedValue({ items: [], total: 0 });
    render(await PortfolioActivityPage());
    expect(screen.getByRole("link", { name: "Connect a broker" })).toBeInTheDocument();
  });

  it("treats an unreachable catalog as not connected, not as connected", async () => {
    mockedCatalog.mockRejectedValue(new Error("api down"));
    mockedOverview.mockResolvedValue(null);
    mockedActivity.mockResolvedValue(null);
    render(await PortfolioActivityPage());
    expect(screen.getByRole("link", { name: "Connect a broker" })).toBeInTheDocument();
  });

  it("says the feed is missing when the read failed on a connected account", async () => {
    mockedCatalog.mockResolvedValue(catalog(true));
    mockedOverview.mockResolvedValue(overview("Holdings synced: 2026-09-11"));
    mockedActivity.mockResolvedValue(null);
    render(await PortfolioActivityPage());
    expect(screen.getByTestId("activity-unavailable")).toHaveTextContent("did not load");
  });
});
