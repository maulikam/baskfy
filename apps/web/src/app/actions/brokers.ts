"use server";

import { revalidatePath } from "next/cache";

import {
  startBrokerConnect,
  syncBrokerHoldings,
  type ConnectResult,
  type SyncHoldingsResult,
} from "@/lib/brokers/fetch";

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

/** Sync holdings from a client click — the bearer stays on the server, as with connect. */
export async function syncHoldingsAction(brokerId: string): Promise<SyncHoldingsResult> {
  const trimmed = brokerId.trim();
  if (!trimmed) {
    return {
      broker_id: "",
      persisted: false,
      written: 0,
      unresolved: [],
      source: "empty",
      degraded: false,
      note: "Pick a broker.",
      sync_note: "Pick a broker.",
    };
  }
  const result = await syncBrokerHoldings(trimmed);
  // Holdings feed every Portfolio surface; without this they keep serving the cached empty state.
  revalidatePath("/portfolio", "layout");
  return result;
}
