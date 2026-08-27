"use server";

import type { CheckoutSessionOut } from "@baskfy/api-client";

import { serverApiOrigin } from "@/lib/api/config";
import { auth } from "@/lib/auth";

/**
 * Server actions for `/pricing` — docs/07: `POST /checkout/session { plan_code }`.
 *
 * The plan code is the *only* thing the browser gets to choose. The amount, the currency and the
 * gateway's order or subscription id all come back from the API, which reads them from the plan
 * row — a browser that posts an amount is a browser that sets its own price.
 */

export type CheckoutResult =
  | { ok: true; session: CheckoutSessionOut }
  | { ok: false; message: string };

const UNREACHABLE = "We could not reach the billing service. Try again in a moment.";

export async function startCheckout(planCode: string): Promise<CheckoutResult> {
  const session = await auth();
  const token = session?.accessToken;
  if (!token) return { ok: false, message: "Sign in first." };

  try {
    const response = await fetch(`${serverApiOrigin()}/api/v1/checkout/session`, {
      method: "POST",
      headers: { "content-type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify({ plan_code: planCode }),
      cache: "no-store",
    });
    if (response.ok) {
      return { ok: true, session: (await response.json()) as CheckoutSessionOut };
    }
    const problem = (await response.json().catch(() => null)) as { detail?: string } | null;
    return { ok: false, message: problem?.detail ?? "That did not work." };
  } catch {
    return { ok: false, message: UNREACHABLE };
  }
}
