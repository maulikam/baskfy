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

/** Persist a PRIVATE basket. Throws {@link CreateBasketError} on non-2xx. */
export async function createPrivateBasket(
  body: CreateBasketRequest,
): Promise<CreateBasketResponse> {
  const token = await accessToken();
  if (!token) {
    throw new CreateBasketError("Sign in to save a private basket.", 401);
  }

  const response = await fetch(`${apiOrigin()}/api/v1/cb/baskets`, {
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

  return (await response.json()) as CreateBasketResponse;
}
