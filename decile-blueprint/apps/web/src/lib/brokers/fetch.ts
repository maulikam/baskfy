import "server-only";

import { serverApiOrigin } from "@/lib/api/config";
import { auth } from "@/lib/auth";

/**
 * Server-side reads for `/brokers` — M41.
 *
 * The catalog is always fetchable for a signed-in account. Live OAuth is not: the payload's
 * `gate.live_oauth_enabled` is the server's own answer to whether D3 has been signed off.
 */

export interface BrokerCapabilities {
  oauth: string;
  holdings_sync: string;
  trading: string;
}

export interface Broker {
  id: string;
  name: string;
  short_name: string;
  mark: string;
  color: string;
  blurb: string;
  api_name: string;
  docs_url: string;
  capabilities: BrokerCapabilities;
  sort_order: number;
  connected: boolean;
  connection_status: string;
  adapter_wired: boolean;
}

export interface BrokerGate {
  live_oauth_enabled: boolean;
  requirement: string;
  signed_off: boolean;
  decision_reference: string;
  /**
   * Whether this deployment holds Kite **Connect** credentials — separate from the D3 policy
   * gate above, which says only that connecting is *allowed*. Baskfy runs on Kite Publisher,
   * which issues no API secret, so this is false and the grid offers no login it cannot finish.
   */
  connect_configured: boolean;
}

export interface BrokerCatalog {
  gate: BrokerGate;
  brokers: Broker[];
  adapters_wired: number;
}

export interface ConnectResult {
  broker_id: string;
  oauth_available: boolean;
  redirect_url: string | null;
  state: string | null;
  reason: string;
}

export class BrokersUnavailable extends Error {}

export async function fetchBrokerCatalog(): Promise<BrokerCatalog> {
  const session = await auth();
  if (!session?.accessToken) throw new BrokersUnavailable("sign-in required");

  const response = await fetch(`${serverApiOrigin()}/api/v1/brokers`, {
    headers: { Authorization: `Bearer ${session.accessToken}` },
    cache: "no-store",
  });
  if (!response.ok) throw new BrokersUnavailable(`brokers ${response.status}`);
  return (await response.json()) as BrokerCatalog;
}

export async function startBrokerConnect(brokerId: string): Promise<ConnectResult> {
  const session = await auth();
  if (!session?.accessToken) {
    return {
      broker_id: brokerId,
      oauth_available: false,
      redirect_url: null,
      state: null,
      reason: "Sign in to connect a broker.",
    };
  }

  const response = await fetch(`${serverApiOrigin()}/api/v1/brokers/${brokerId}/connect`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${session.accessToken}`,
      "Content-Type": "application/json",
    },
    cache: "no-store",
  });
  if (!response.ok) {
    return {
      broker_id: brokerId,
      oauth_available: false,
      redirect_url: null,
      state: null,
      reason: `Could not start connect (${response.status}).`,
    };
  }
  return (await response.json()) as ConnectResult;
}
