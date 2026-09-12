import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import PortfolioActivityPage from "../page";

/**
 * M83. The empty state was static: "Once a broker is connected…" over a "Connect a broker" link,
 * shown to everyone. Maulik read it with Zerodha connected and 17 holdings synced.
 *
 * Audit 1.3: connected ≠ synced. The page must render `overview.sync_summary`, not invent
 * "holdings are synced" from a connected OAuth token.
 */
vi.mock("@/lib/brokers/fetch", () => ({
  fetchBrokerCatalog: vi.fn(),
}));

vi.mock("@/lib/portfolio/fetch", () => ({
  fetchPortfolioOverview: vi.fn(),
}));

// `SectionTabs` reads `usePathname`, which is null outside a router. The tabs are not what these
// tests are about; the empty state is.
vi.mock("next/navigation", () => ({
  usePathname: () => "/portfolio/activity",
}));

const { fetchBrokerCatalog } = await import("@/lib/brokers/fetch");
const { fetchPortfolioOverview } = await import("@/lib/portfolio/fetch");
const mockedCatalog = vi.mocked(fetchBrokerCatalog);
const mockedOverview = vi.mocked(fetchPortfolioOverview);

const catalog = (connected: boolean) =>
  ({ brokers: [{ id: "zerodha", connected }] }) as unknown as Awaited<
    ReturnType<typeof fetchBrokerCatalog>
  >;

describe("the Activity empty state", () => {
  it("does not tell a connected account to connect", async () => {
    mockedCatalog.mockResolvedValue(catalog(true));
    mockedOverview.mockResolvedValue({
      sync_summary: "Holdings not synced yet",
      live_overlay: false,
    } as Awaited<ReturnType<typeof fetchPortfolioOverview>>);
    render(await PortfolioActivityPage());
    expect(screen.queryByRole("link", { name: "Connect a broker" })).not.toBeInTheDocument();
    expect(screen.getByTestId("sync-summary")).toHaveTextContent("Holdings not synced yet");
  });

  it("does not invent synced from a connected broker alone", async () => {
    mockedCatalog.mockResolvedValue(catalog(true));
    mockedOverview.mockResolvedValue({
      sync_summary: "primary has never synced",
      live_overlay: false,
    } as Awaited<ReturnType<typeof fetchPortfolioOverview>>);
    render(await PortfolioActivityPage());
    expect(screen.getByTestId("sync-summary")).toHaveTextContent("primary has never synced");
    expect(screen.getByTestId("activity-empty")).not.toHaveTextContent("holdings are synced");
  });

  it("says plainly that syncing again will not help", async () => {
    // The feed does not exist. Implying otherwise is what sent Maulik back to /brokers.
    mockedCatalog.mockResolvedValue(catalog(true));
    mockedOverview.mockResolvedValue({
      sync_summary: "Holdings synced: 2026-09-11",
      live_overlay: false,
    } as Awaited<ReturnType<typeof fetchPortfolioOverview>>);
    render(await PortfolioActivityPage());
    expect(screen.getByTestId("activity-empty")).toHaveTextContent("not recorded yet");
  });

  it("still offers Connect when nothing is connected", async () => {
    mockedCatalog.mockResolvedValue(catalog(false));
    mockedOverview.mockResolvedValue(null);
    render(await PortfolioActivityPage());
    expect(screen.getByRole("link", { name: "Connect a broker" })).toBeInTheDocument();
  });

  it("treats an unreachable catalog as not connected, not as connected", async () => {
    // The cautious direction: offer a link rather than assert a session that may not exist.
    mockedCatalog.mockRejectedValue(new Error("api down"));
    mockedOverview.mockResolvedValue(null);
    render(await PortfolioActivityPage());
    expect(screen.getByRole("link", { name: "Connect a broker" })).toBeInTheDocument();
  });
});
