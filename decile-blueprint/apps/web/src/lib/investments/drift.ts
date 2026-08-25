"use client";

import { isProblem } from "@baskfy/api-client";

import { accessToken } from "@/lib/api/browser";
import { apiOrigin } from "@/lib/api/config";

/**
 * Client POSTs for T8.6 drift scan/fix. Rebases the intended ledger. Never an order route.
 */

export interface DriftDelta {
  symbol: string;
  ledger_qty: string | number;
  broker_qty: string | number;
  shortfall: string | number;
  excess: string | number;
}

export interface DriftScanResult {
  action_type: string | null;
  deltas: DriftDelta[];
  pending_action_id: number | null;
}

export interface DriftFixResult {
  holdings: Array<{ symbol: string; qty: string | number }>;
  cleared_action: boolean;
  synthetic_exit_count: number;
}

export class DriftError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "DriftError";
    this.status = status;
  }
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const token = await accessToken();
  if (!token) {
    throw new DriftError("Sign in to compare holdings.", 401);
  }
  const response = await fetch(`${apiOrigin()}/api/v1${path}`, {
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
      : `Could not update the ledger (${response.status}).`;
    throw new DriftError(detail, response.status);
  }
  return (await response.json()) as T;
}

export async function scanDrift(
  investmentId: string,
  brokerHoldings: Array<{ symbol: string; qty: number }>,
): Promise<DriftScanResult> {
  return postJson(
    `/cb/investments/${encodeURIComponent(investmentId)}/drift/scan`,
    { broker_holdings: brokerHoldings },
  );
}

export async function fixDrift(
  investmentId: string,
  brokerHoldings: Array<{ symbol: string; qty: number }>,
): Promise<DriftFixResult> {
  return postJson(
    `/cb/investments/${encodeURIComponent(investmentId)}/drift/fix`,
    { broker_holdings: brokerHoldings },
  );
}

/** Parse `SYMBOL QTY` lines. */
export function parseBrokerLines(text: string): Array<{ symbol: string; qty: number }> {
  const rows: Array<{ symbol: string; qty: number }> = [];
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    const parts = line.split(/[\s,]+/);
    if (parts.length < 2) {
      throw new DriftError(`Each line is SYMBOL QTY — could not read “${line}”.`, 400);
    }
    const [symbolRaw, qtyRaw] = parts;
    if (symbolRaw === undefined || qtyRaw === undefined) {
      throw new DriftError(`Each line is SYMBOL QTY — could not read “${line}”.`, 400);
    }
    const symbol = symbolRaw.toUpperCase();
    const qty = Number(qtyRaw);
    if (!Number.isFinite(qty) || qty < 0) {
      throw new DriftError(`qty must be a number on “${line}”.`, 400);
    }
    rows.push({ symbol, qty });
  }
  return rows;
}
