import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import PortfolioActivityPage from "../page";

/**
 * M83. The empty state was static: "Once a broker is connected…" over a "Connect a broker" link,
 * shown to everyone. Maulik read it with Zerodha connected and 17 holdings synced.
 *
 * Two untruths in one box. It implied he was not connected when he was, and it implied connecting
 * would fill the page — when nothing would, because the activity feed does not exist. Prompting an
 * action that cannot help is worse than an empty box: it sends someone to redo work already done.
 */
vi.mock("@/lib/brokers/fetch", () => ({
  fetchBrokerCatalog: vi.fn(),
}));

// `SectionTabs` reads `usePathname`, which is null outside a router. The tabs are not what these
// tests are about; the empty state is.
vi.mock("next/navigation", () => ({
  usePathname: () => "/portfolio/activity",
}));

const { fetchBrokerCatalog } = await import("@/lib/brokers/fetch");
const mocked = vi.mocked(fetchBrokerCatalog);

const catalog = (connected: boolean) =>
  ({ brokers: [{ id: "zerodha", connected }] }) as unknown as Awaited<
    ReturnType<typeof fetchBrokerCatalog>
  >;

describe("the Activity empty state", () => {
  it("does not tell a connected account to connect", async () => {
    mocked.mockResolvedValue(catalog(true));
    render(await PortfolioActivityPage());
    expect(screen.queryByRole("link", { name: "Connect a broker" })).not.toBeInTheDocument();
    expect(screen.getByTestId("activity-empty")).toHaveTextContent("broker is connected");
  });

  it("says plainly that syncing again will not help", async () => {
    // The feed does not exist. Implying otherwise is what sent Maulik back to /brokers.
    mocked.mockResolvedValue(catalog(true));
    render(await PortfolioActivityPage());
    expect(screen.getByTestId("activity-empty")).toHaveTextContent("not recorded yet");
  });

  it("still offers Connect when nothing is connected", async () => {
    mocked.mockResolvedValue(catalog(false));
    render(await PortfolioActivityPage());
    expect(screen.getByRole("link", { name: "Connect a broker" })).toBeInTheDocument();
  });

  it("treats an unreachable catalog as not connected, not as connected", async () => {
    // The cautious direction: offer a link rather than assert a session that may not exist.
    mocked.mockRejectedValue(new Error("api down"));
    render(await PortfolioActivityPage());
    expect(screen.getByRole("link", { name: "Connect a broker" })).toBeInTheDocument();
  });
});
