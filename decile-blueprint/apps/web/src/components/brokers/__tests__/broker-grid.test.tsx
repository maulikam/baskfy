import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/app/actions/brokers", () => ({ connectBrokerAction: vi.fn() }));

import { BrokerGrid } from "@/components/brokers/broker-grid";
import type { Broker, BrokerGate } from "@/lib/brokers/fetch";

/**
 * An affordance that cannot succeed is worse than no affordance.
 *
 * Baskfy runs on Kite **Publisher** — the free product that hands a basket to the user's own Kite
 * for them to confirm. Publisher issues no API secret, and the login "Connect Zerodha" starts ends
 * at `session/token`, whose checksum needs one. So on this deployment that button could only ever
 * return an error about a missing credential, and it did: twice, to Maulik, each time after he had
 * pasted a perfectly good Publisher key. What he learned from it was that the product was broken,
 * which is the wrong lesson and not even true. `NEEDS-MAULIK.md` §28.
 */
const ZERODHA: Broker = {
  id: "zerodha",
  name: "Zerodha",
  short_name: "Zerodha",
  mark: "Z",
  color: "#387ed1",
  blurb: "Kite Connect — holdings, positions and CNC orders in your own account.",
  api_name: "Kite Connect",
  docs_url: "https://kite.trade/docs/connect/v3/",
  capabilities: { oauth: "ready", holdings_sync: "ready", trading: "ready" },
  sort_order: 1,
  connected: false,
  connection_status: "not_connected",
  adapter_wired: true,
};

function gate(overrides: Partial<BrokerGate> = {}): BrokerGate {
  return {
    live_oauth_enabled: true,
    requirement: "posture B",
    signed_off: true,
    decision_reference: "D3",
    connect_configured: false,
    ...overrides,
  };
}

describe("when this deployment has no Kite Connect credentials", () => {
  it("offers no Connect button at all", () => {
    render(<BrokerGrid brokers={[ZERODHA]} gate={gate()} />);
    expect(screen.queryByRole("button", { name: /^Connect Zerodha$/ })).toBeNull();
  });

  it("says what the user should do instead, in the product's own terms", () => {
    /* Not "unavailable". A reader who is told a thing is unavailable waits for it; a reader who
       is told they do not need it goes and does the thing that works. */
    render(<BrokerGrid brokers={[ZERODHA]} gate={gate()} />);
    expect(screen.getByText(/don.t need to connect Zerodha to invest/i)).toBeInTheDocument();
    expect(screen.getByText(/hands it to your own Kite/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /browse baskets/i })).toBeInTheDocument();
  });

  it("still promises that Baskfy holds no broker credentials", () => {
    render(<BrokerGrid brokers={[ZERODHA]} gate={gate()} />);
    expect(screen.getByText(/never holds your broker credentials/i)).toBeInTheDocument();
  });
});

describe("when Connect credentials are present", () => {
  it("offers the button, because the login can now finish", () => {
    render(<BrokerGrid brokers={[ZERODHA]} gate={gate({ connect_configured: true })} />);
    expect(screen.getByRole("button", { name: /^Connect Zerodha$/ })).toBeInTheDocument();
  });

  it("keeps the promise that this page never executes", () => {
    render(<BrokerGrid brokers={[ZERODHA]} gate={gate({ connect_configured: true })} />);
    expect(screen.getByText(/this page never executes/i)).toBeInTheDocument();
  });
});

describe("the D3 policy gate and the credential are different questions", () => {
  it("a signed-off posture with no credential still shows no button", () => {
    /* These were conflated. D3 says connecting is *allowed*; `connect_configured` says we have
       the credential to do it. Allowed-but-impossible is exactly the state that produced the
       error message, so the two must be asked separately. */
    render(
      <BrokerGrid brokers={[ZERODHA]} gate={gate({ live_oauth_enabled: true, signed_off: true })} />,
    );
    expect(screen.queryByRole("button", { name: /^Connect Zerodha$/ })).toBeNull();
  });
});
