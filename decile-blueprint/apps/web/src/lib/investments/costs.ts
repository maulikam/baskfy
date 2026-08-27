import "server-only";

import { auth } from "@/lib/auth";
import { serverApiOrigin } from "@/lib/api/config";
import { serverFetchJsonOrNull } from "@/lib/api/server-fetch";

/**
 * Server read for T8.5 costs-and-returns. Accrued fees only — no collection.
 */

export interface CostsSnapshot {
  money_put_in: string;
  current_investment: string;
  current_value: string;
  current_returns: string;
  current_returns_pct: string;
  realized_pnl: string;
  dividends: string;
  xirr: string | null;
  xirr_displayable: boolean;
}

export interface CostsPayload {
  snapshot: CostsSnapshot;
  accrued_fees_total: string;
  returns_after_fees: string;
  collected: boolean;
}

export async function fetchInvestmentCosts(id: string): Promise<CostsPayload | null> {
  const session = await auth();
  const token = session?.accessToken;
  const data = await serverFetchJsonOrNull({
    url: `${serverApiOrigin()}/api/v1/cb/investments/${encodeURIComponent(id)}/costs`,
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (data === null) return null;
  return data as CostsPayload;
}
