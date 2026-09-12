"use client";

import { isProblem } from "@baskfy/api-client";

import { accessToken } from "@/lib/api/browser";
import { apiOrigin } from "@/lib/api/config";

/**
 * Client POST for SC8 `/create` → `POST /api/v1/cb/baskets` (leaf 3.4 / leaf 2.2 API).
 *
 * Bearer auth via Auth.js session token — same pattern as instrument search / export.
 * No order path: create persists a PRIVATE MANUAL basket only.
 */

export interface CreateBasketConstituentIn {
  symbol: string;
  weight: number;
}

export interface CreateBasketRequest {
  name: string;
  constituents: CreateBasketConstituentIn[];
  description_md?: string | null;
}

export interface CreateBasketConstituentOut {
  symbol: string;
  instrument_id: number;
  weight: string | number;
  segment: string;
}

export interface CreateBasketResponse {
  id: number;
  slug: string;
  name: string;
  visibility: string;
  type: string;
  source: string;
  version_no: number;
  label: string;
  constituents: CreateBasketConstituentOut[];
}

export class CreateBasketError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "CreateBasketError";
    this.status = status;
  }
}

export interface CreateFromScreenRequest {
  screen_public_id: string;
  amount: number;
  name?: string | null;
  /** Suggests the name count. Ignored by the server when `holdings` is given. */
  profile?: string | null;
  /** The investor's own count, which beats the profile's suggestion. */
  holdings?: number | null;
  /** The desk's exposure tier (R1–R4), which decides the cash share — never the count. */
  exposure_tier?: string | null;
  /** Explicit cash percent (0–95). Beats the tier suggestion when set. */
  cash_pct?: number | null;
  /** How to split the deployed money. Defaults to equal on the server. */
  method?: string | null;
  /**
   * Only when `method` is CUSTOM. The server still runs the screen; these numbers cannot
   * add or drop a name. Named `custom_weights` so this is not the caller supplying the basket.
   */
  custom_weights?: { symbol: string; weight: number }[] | null;
}

export interface CreateFromScreenHolding {
  rank: number;
  symbol: string;
  instrument_id: number;
  weight: string | number;
  weight_pct_of_amount: string | number;
  amount: string | number;
}

export interface CreateFromScreenResponse {
  id: number;
  slug: string;
  name: string;
  visibility: string;
  type: string;
  source: string;
  version_no: number;
  label: string;
  screen_public_id: string;
  as_of: string;
  amount: string | number;
  deployed: string | number;
  cash: string | number;
  cash_pct: string | number;
  profile: string | null;
  holdings_overridden: boolean;
  minimum_amount: string | number | null;
  fundable: boolean;
  method: string;
  holdings: CreateFromScreenHolding[];
}

async function post<T>(path: string, body: unknown, whenAnonymous: string): Promise<T> {
  const token = await accessToken();
  if (!token) {
    throw new CreateBasketError(whenAnonymous, 401);
  }

  const response = await fetch(`${apiOrigin()}${path}`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
    cache: "no-store",
  });

  if (!response.ok) {
    const payload: unknown = await response.json().catch(() => null);
    const detail = isProblem(payload)
      ? payload.detail
      : `Could not save basket (${response.status}).`;
    throw new CreateBasketError(detail, response.status);
  }

  return (await response.json()) as T;
}

/** Persist a PRIVATE basket. Throws {@link CreateBasketError} on non-2xx. */
export async function createPrivateBasket(
  body: CreateBasketRequest,
): Promise<CreateBasketResponse> {
  // `/cb/baskets`, not `/cb/discover`: M48's `baskets` -> `discover` page-tree rename swept this
  // API path along with it (see `lib/basket/fetch.ts`). The route is curated_create.py's
  // `POST /cb/baskets`, which has never been renamed.
  return post<CreateBasketResponse>(
    "/api/v1/cb/baskets",
    body,
    "Sign in to save a private basket.",
  );
}

/**
 * Save a screen as a basket (SB1).
 *
 * Deliberately sends no symbols: the server runs the named screen itself, so what it stores is
 * the screen's own output rather than whatever this browser happened to be showing. The response
 * is therefore the authoritative sizing, and can differ from the on-screen preview if the
 * published data moved in between.
 */
export async function createBasketFromScreen(
  body: CreateFromScreenRequest,
): Promise<CreateFromScreenResponse> {
  return post<CreateFromScreenResponse>(
    "/api/v1/cb/baskets/from-screen",
    body,
    "Sign in to save this screen as a basket.",
  );
}
