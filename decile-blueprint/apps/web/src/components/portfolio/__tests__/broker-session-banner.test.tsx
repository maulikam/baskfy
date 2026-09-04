import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { BrokerSessionBanner } from "@/components/portfolio/broker-session-banner";

/**
 * M84. Kite ends a session every trading day, so an expired broker is the normal morning state.
 * M79 taught /brokers to say so; the portfolio pages went quietly stale instead, because a stale
 * number and a fresh one look identical.
 */
vi.mock("@/lib/brokers/fetch", () => ({ fetchBrokerCatalog: vi.fn() }));
const { fetchBrokerCatalog } = await import("@/lib/brokers/fetch");
const mocked = vi.mocked(fetchBrokerCatalog);

const catalog = (status: string) =>
  ({ brokers: [{ short_name: "Zerodha", connection_status: status }] }) as unknown as Awaited<
    ReturnType<typeof fetchBrokerCatalog>
  >;

describe("the expired-session banner", () => {
  it("warns when the session has expired, and says why", async () => {
    mocked.mockResolvedValue(catalog("expired"));
    render(await BrokerSessionBanner());
    expect(screen.getByTestId("session-expired-banner")).toHaveTextContent("session expired");
    expect(screen.getByRole("link", { name: "reconnect" })).toBeInTheDocument();
  });

  it("says nothing when the session is healthy", async () => {
    // A banner that appears when nothing is wrong is one people learn to scroll past.
    mocked.mockResolvedValue(catalog("connected"));
    const ui = await BrokerSessionBanner();
    expect(ui).toBeNull();
  });

  it("says nothing when no broker was ever connected", async () => {
    // Not a problem to interrupt someone about; the empty states already explain themselves.
    mocked.mockResolvedValue(catalog("not_connected"));
    expect(await BrokerSessionBanner()).toBeNull();
  });

  it("stays quiet when the catalog cannot be read", async () => {
    // An unreachable API is not evidence of an expired session.
    mocked.mockRejectedValue(new Error("api down"));
    expect(await BrokerSessionBanner()).toBeNull();
  });
});
