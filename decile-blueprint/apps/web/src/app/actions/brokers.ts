"use server";

import { startBrokerConnect, type ConnectResult } from "@/lib/brokers/fetch";

/** Start a broker OAuth redirect from a client click — bearer stays on the server. */
export async function connectBrokerAction(brokerId: string): Promise<ConnectResult> {
  const trimmed = brokerId.trim();
  if (!trimmed) {
    return {
      broker_id: "",
      oauth_available: false,
      redirect_url: null,
      state: null,
      reason: "Pick a broker.",
    };
  }
  return startBrokerConnect(trimmed);
}
