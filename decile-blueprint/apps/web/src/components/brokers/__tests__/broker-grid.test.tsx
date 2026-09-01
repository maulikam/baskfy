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
  blurb: "Kite Publisher — send a basket to your own Kite and confirm it there.",
  api_name: "Kite Publisher",
  docs_url: "https://kite.trade/docs/connect/v3/publisher/",
  capabilities: { oauth: "not_applicable", holdings_sync: "not_available", trading: "handoff" },
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

  it("does not repeat the page banner's message in the detail panel", () => {
    /* The panel used to restate "You don't need to connect Zerodha to invest" with the whole
       hand-off explanation under it. The page banner already opens with that, so the detail panel
       said it a second time at greater length, directly under a heading naming the broker — which
       reads as an apology for a missing feature rather than a design (Maulik, 29 Aug 2026).

       What a reader needs is still on screen: the capability rows below. */
    render(<BrokerGrid brokers={[ZERODHA]} gate={gate()} />);
    expect(screen.queryByText(/don.t need to connect Zerodha to invest/i)).toBeNull();
    expect(screen.queryByRole("link", { name: /browse baskets/i })).toBeNull();
  });

  it("still tells the reader what this broker can and cannot do", () => {
    /* Removing the panel must not remove the answer. These three rows are what is left, and they
       are the honest version of it. */
    render(<BrokerGrid brokers={[ZERODHA]} gate={gate()} />);
    expect(screen.getByText("Not needed")).toBeInTheDocument();
    expect(screen.getByText("Not available")).toBeInTheDocument();
    expect(screen.getByText("You confirm in Kite")).toBeInTheDocument();
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


describe("the panel describes Publisher, not a Connect OAuth", () => {
  /*
   * The over-claim Maulik kept hitting. Zerodha's row said "Kite Connect — holdings, positions and
   * CNC orders", three green "Ready" labels, and three steps describing an OAuth login: "Authorize
   * Baskfy to read holdings", "we sync the holdings into your portfolio". None of it is true of the
   * integration Baskfy uses. Kite Publisher links no account, stores no token and reads nothing
   * back — and Kite Connect is ₹2,000/month licensed for the app owner's own account, which is the
   * wrong shape for a product other people sign into. `docs/DECISIONS-MERGE.md` M55.
   */
  it("names Kite Publisher", () => {
    render(<BrokerGrid brokers={[ZERODHA]} gate={gate()} />);
    expect(screen.getByText("Kite Publisher")).toBeInTheDocument();
    expect(screen.queryByText("Kite Connect")).toBeNull();
  });

  it("does not promise a holdings sync it cannot do", () => {
    render(<BrokerGrid brokers={[ZERODHA]} gate={gate()} />);
    expect(screen.getByText("Not available")).toBeInTheDocument();
    // The old rendering: a green "Ready" beside `holdings_sync`.
    expect(screen.queryByText("Ready")).toBeNull();
  });

  it("calls the account link what it is — nothing to link", () => {
    render(<BrokerGrid brokers={[ZERODHA]} gate={gate()} />);
    expect(screen.getByText("Account link")).toBeInTheDocument();
    expect(screen.getByText("Not needed")).toBeInTheDocument();
    expect(screen.getByText("You confirm in Kite")).toBeInTheDocument();
    // The tile says what the broker *does*, not "Not needed" — which is a true answer to a
    // question nobody asked while looking at a grid of logos.
    expect(screen.getByText("Basket hand-off")).toBeInTheDocument();
  });

  it("gives the steps of the flow it actually has", () => {
    render(<BrokerGrid brokers={[ZERODHA]} gate={gate()} />);
    expect(screen.getByText(/Pick a basket and the amount/i)).toBeInTheDocument();
    expect(screen.getByText(/opens them in Zerodha as one basket/i)).toBeInTheDocument();
    expect(screen.getByText(/nothing is placed from Baskfy/i)).toBeInTheDocument();
    // The Connect-flow steps must not appear for a hand-off broker.
    expect(screen.queryByText(/Authorize Baskfy to read holdings/i)).toBeNull();
    expect(screen.queryByText(/we sync the holdings into your portfolio/i)).toBeNull();
  });

  it("still shows the OAuth steps for a broker that really does connect", () => {
    /* The other integration is not deleted — the code path exists and a future broker may use it.
       Asserting it keeps this from becoming a one-way door. */
    const oauthBroker = {
      ...ZERODHA,
      id: "somebroker",
      short_name: "SomeBroker",
      capabilities: { oauth: "ready", holdings_sync: "ready", trading: "ready" },
    };
    render(<BrokerGrid brokers={[oauthBroker]} gate={gate({ connect_configured: true })} />);
    expect(screen.getByText(/Authorize Baskfy to read holdings/i)).toBeInTheDocument();
  });
});

describe("a connected broker (M76)", () => {
  /**
   * Maulik reported "still says Connect Zerodha" twice. M75 made the API report `connected` and
   * the TILE read it — his own screenshot shows the tile saying "Connected". The panel did not:
   * it rendered `Connect {short_name}` unconditionally, so the button he actually clicks was
   * untouched by the fix. Reading a field in one of the two places it is shown is not reading it.
   */
  const connected = { ...ZERODHA, connected: true, connection_status: "connected" };

  it("says it is connected instead of offering Connect as the main action", () => {
    render(<BrokerGrid brokers={[connected]} gate={gate({ connect_configured: true })} />);
    expect(screen.getByTestId("broker-connected")).toHaveTextContent("Connected to Zerodha");
    expect(screen.queryByRole("button", { name: "Connect Zerodha" })).not.toBeInTheDocument();
  });

  it("offers Sync holdings, which is what was missing", () => {
    // The endpoint has persisted since M75 and no screen could call it, so portfolio_holding
    // stayed at 0 and every Portfolio surface said "Holdings not synced yet".
    render(<BrokerGrid brokers={[connected]} gate={gate({ connect_configured: true })} />);
    expect(screen.getByRole("button", { name: "Sync holdings" })).toBeInTheDocument();
  });

  it("still allows a deliberate reconnect, demoted so it is not the obvious click", () => {
    // Re-running connect issues a fresh Kite token and invalidates the working session, so it
    // must stay reachable but must not be the primary button.
    render(<BrokerGrid brokers={[connected]} gate={gate({ connect_configured: true })} />);
    expect(screen.getByRole("button", { name: "Reconnect Zerodha" })).toBeInTheDocument();
  });

  it("a disconnected broker is unchanged — Connect is still the action", () => {
    render(<BrokerGrid brokers={[ZERODHA]} gate={gate({ connect_configured: true })} />);
    expect(screen.getByRole("button", { name: "Connect Zerodha" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sync holdings" })).not.toBeInTheDocument();
  });
});

describe("an expired Kite session (M79)", () => {
  /**
   * Kite invalidates an access token at the start of the next trading day. On the morning of
   * 2 Sep 2026 the API still reported `connected` from a token written the night before, while
   * holdings were already answering `AccessTokenExpired` and serving a fixture — the page claiming
   * a session the server did not have, which is the defect this whole area exists to fix.
   */
  const expired = { ...ZERODHA, connected: false, connection_status: "expired" };

  it("explains why, rather than silently offering Connect again", () => {
    render(<BrokerGrid brokers={[expired]} gate={gate({ connect_configured: true })} />);
    expect(screen.getByTestId("broker-expired")).toHaveTextContent("session expired");
    expect(screen.getByRole("button", { name: "Reconnect Zerodha" })).toBeInTheDocument();
  });

  it("does not offer Sync, which would only refuse", () => {
    // A sync on a dead session reads a fixture, and a fixture is never persisted.
    render(<BrokerGrid brokers={[expired]} gate={gate({ connect_configured: true })} />);
    expect(screen.queryByRole("button", { name: "Sync holdings" })).not.toBeInTheDocument();
  });

  it("a never-connected broker says nothing about expiry", () => {
    render(<BrokerGrid brokers={[ZERODHA]} gate={gate({ connect_configured: true })} />);
    expect(screen.queryByTestId("broker-expired")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Connect Zerodha" })).toBeInTheDocument();
  });
});
