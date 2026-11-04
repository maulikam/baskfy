import { accessToken } from "@/lib/api/browser";
import { apiOrigin } from "@/lib/api/config";
import { ApiError } from "@/lib/api/errors";
import { forestNodes } from "@/lib/portfolios/tree";

import type { PortfolioNodeOut } from "@baskfy/api-client";

/** House MTF factor when the investor does not pick one (SCREEN-INVEST-RS). */
export const DEFAULT_LEVERAGE = 4;
export const DEFAULT_INVEST_AMOUNT = 100_000;

export interface ScreenInvestHolding {
  symbol: string;
  name: string;
  rank: number;
  quantity: number | null;
  amount: string;
  weight: string;
  price: string | null;
  leverage: string;
  product: "MTF" | "CNC";
}

export interface ScreenInvestSkip {
  symbol: string;
  name: string;
  reason: "mtf_ineligible" | "no_price" | string;
}

export interface ScreenInvestPreview {
  as_of: string | null;
  cash_outlay: string;
  exposure: string;
  leverage: string;
  mtf: boolean;
  min_investment: string;
  leftover: string;
  empty_reason: string | null;
  holdings: ScreenInvestHolding[];
  skipped: ScreenInvestSkip[];
  report: { portfolio: { id: number; name: string } } | null;
  orders?: ScreenOrderResult[];
}

export interface ScreenOrderResult {
  symbol: string;
  status: string;
  order_id: string | null;
  stop: string | null;
  error: string | null;
}

export interface ScreenInvestRequest {
  name?: string;
  amount: number;
  mtf: boolean;
  leverage?: number;
}

function asPreview(payload: unknown, fallback: string, status?: number): ScreenInvestPreview {
  if (payload && typeof payload === "object" && "holdings" in payload) {
    return payload as ScreenInvestPreview;
  }
  throw ApiError.from(payload, fallback, status);
}

async function screenInvestFetch(
  path: string,
  method: "POST",
  body: Record<string, unknown>,
  fallback: string,
): Promise<ScreenInvestPreview> {
  const token = await accessToken();
  const response = await fetch(`${apiOrigin()}${path}`, {
    method,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify(body),
  });
  const payload: unknown = await response.json().catch(() => null);
  if (
    !response.ok &&
    payload &&
    typeof payload === "object" &&
    "holdings" in payload &&
    "orders" in payload
  ) {
    return asPreview(payload, fallback, response.status);
  }
  if (!response.ok) {
    throw ApiError.from(payload, fallback, response.status);
  }
  return asPreview(payload, fallback, response.status);
}

export function previewScreenInvest(
  screenPublicId: string,
  input: ScreenInvestRequest,
): Promise<ScreenInvestPreview> {
  return screenInvestFetch(
    `/api/v1/screens/${encodeURIComponent(screenPublicId)}/invest-preview`,
    "POST",
    {
      amount: input.amount,
      mtf: input.mtf,
      ...(input.leverage === undefined ? {} : { leverage: input.leverage }),
    },
    "The invest preview could not be calculated.",
  );
}

export function confirmScreenInvest(
  screenPublicId: string,
  input: ScreenInvestRequest & { name: string },
): Promise<ScreenInvestPreview> {
  return screenInvestFetch(
    `/api/v1/screens/${encodeURIComponent(screenPublicId)}/invest`,
    "POST",
    {
      name: input.name,
      amount: input.amount,
      mtf: input.mtf,
      ...(input.leverage === undefined ? {} : { leverage: input.leverage }),
    },
    "The portfolio could not be created.",
  );
}

export function rebalanceFromScreen(
  portfolioId: number,
  input: ScreenInvestRequest,
): Promise<ScreenInvestPreview> {
  return screenInvestFetch(
    `/api/v1/portfolios/${portfolioId}/from-screen`,
    "POST",
    {
      amount: input.amount,
      mtf: input.mtf,
      ...(input.leverage === undefined ? {} : { leverage: input.leverage }),
    },
    "The portfolio could not be updated.",
  );
}

export function portfoliosForScreen(
  nodes: readonly PortfolioNodeOut[],
  screenPublicId: string,
): PortfolioNodeOut[] {
  return nodes.filter((node) => node.screen_public_id === screenPublicId);
}

export function linkedPortfoliosForScreen(
  forest: { data: PortfolioNodeOut[]; orphans?: PortfolioNodeOut[] } | null | undefined,
  screenPublicId: string,
): PortfolioNodeOut[] {
  return portfoliosForScreen(forestNodes(forest), screenPublicId);
}

export function nextInvestName(screenName: string, existing: readonly string[]): string {
  const taken = new Set(existing);
  if (!taken.has(screenName)) return screenName;
  for (let n = 2; n < 100; n += 1) {
    const candidate = `${screenName} ${n}`;
    if (!taken.has(candidate)) return candidate;
  }
  return `${screenName} ${existing.length + 1}`;
}
