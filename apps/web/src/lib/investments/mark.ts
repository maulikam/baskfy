"use client";

import { isProblem } from "@baskfy/api-client";

import { accessToken } from "@/lib/api/browser";
import { apiOrigin } from "@/lib/api/config";

/**
 * Client POST for T8.1 mark-as-invested. Records a broker book. Never an order route.
 */

export interface MarkHolding {
  symbol: string;
  qty: number;
  avg_price: number;
}

export interface MarkInvestedRequest {
  basket_slug: string;
  amount: number;
  confirmed: true;
  holdings: MarkHolding[];
  desk_plan_id?: string | null;
}

export interface MarkInvestedResponse {
  id: number;
  status: string;
  basket_slug: string;
  batch: {
    id: number;
    kind: string;
    status: string;
    desk_plan_id: string | null;
    created_at: string | null;
  };
}

export class MarkInvestedError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "MarkInvestedError";
    this.status = status;
  }
}

export async function markInvested(body: MarkInvestedRequest): Promise<MarkInvestedResponse> {
  const token = await accessToken();
  if (!token) {
    throw new MarkInvestedError("Sign in to record an investment.", 401);
  }

  const response = await fetch(`${apiOrigin()}/api/v1/cb/investments/mark`, {
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
      : `Could not record the investment (${response.status}).`;
    throw new MarkInvestedError(detail, response.status);
  }

  return (await response.json()) as MarkInvestedResponse;
}

/** Parse `SYMBOL QTY AVG_PRICE` lines. Blank lines ignored. */
export function parseHoldingLines(text: string): MarkHolding[] {
  const rows: MarkHolding[] = [];
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    const parts = line.split(/[\s,]+/);
    if (parts.length < 3) {
      throw new MarkInvestedError(
        `Each holding is SYMBOL QTY AVG_PRICE — could not read “${line}”.`,
        400,
      );
    }
    const [symbolRaw, qtyRaw, avgRaw] = parts;
    if (symbolRaw === undefined || qtyRaw === undefined || avgRaw === undefined) {
      throw new MarkInvestedError(
        `Each holding is SYMBOL QTY AVG_PRICE — could not read “${line}”.`,
        400,
      );
    }
    const symbol = symbolRaw.toUpperCase();
    const qty = Number(qtyRaw);
    const avg = Number(avgRaw);
    if (!Number.isFinite(qty) || qty <= 0 || !Number.isFinite(avg) || avg < 0) {
      throw new MarkInvestedError(`qty and avg_price must be numbers on “${line}”.`, 400);
    }
    rows.push({ symbol, qty, avg_price: avg });
  }
  return rows;
}
