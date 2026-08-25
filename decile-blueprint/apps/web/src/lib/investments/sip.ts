"use client";

import { isProblem } from "@baskfy/api-client";

import { accessToken } from "@/lib/api/browser";
import { apiOrigin } from "@/lib/api/config";

/**
 * Client POST for T8.4 SIP reminder plans. REMINDER only — never an auto-debit.
 */

export interface SipPlan {
  id: number;
  mode: string;
  status: string;
  amount: string | number;
  day_of_month: number;
  next_fire_date: string;
}

export class SipError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "SipError";
    this.status = status;
  }
}

export async function createSipReminder(
  investmentId: string,
  body: { amount: number; day_of_month: number },
): Promise<SipPlan> {
  const token = await accessToken();
  if (!token) {
    throw new SipError("Sign in to save a SIP reminder.", 401);
  }
  const response = await fetch(
    `${apiOrigin()}/api/v1/cb/investments/${encodeURIComponent(investmentId)}/sip`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ ...body, mode: "REMINDER" }),
      cache: "no-store",
    },
  );
  if (!response.ok) {
    const payload: unknown = await response.json().catch(() => null);
    const detail = isProblem(payload)
      ? payload.detail
      : `Could not save the SIP reminder (${response.status}).`;
    throw new SipError(detail, response.status);
  }
  return (await response.json()) as SipPlan;
}
