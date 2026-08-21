import "server-only";

import type { PlanListOut } from "@baskfy/api-client";

import { apiOrigin } from "@/lib/api/config";
import { billingCadence, formatPrice } from "@/lib/billing/format";

/**
 * The landing page's pricing summary (Prompt 18 deliverable 1: "pricing summary").
 *
 * The same `GET /plans` the pricing page reads, cut down to a name and a price. Prompt 13's fourth
 * acceptance criterion — "No price or entitlement is hard-coded in the web app" — applies to the
 * marketing page exactly as it applies to the checkout page, and
 * `src/lib/__tests__/no-hardcoded-pricing.test.ts` scans this directory too.
 *
 * Unlike `lib/billing/fetch`, a failure is an empty list rather than a throw: the landing page has
 * four other sections and must render without the billing service, whereas a pricing page with no
 * prices on it has nothing to say.
 */
const REVALIDATE_SECONDS = 300;

export interface PlanSummary {
  code: string;
  name: string;
  /** The rupee amount as `lib/billing/format` renders it. Never composed here. */
  display: string;
  cadence: string;
  /** For the `Offer` in the landing page's JSON-LD, which wants a bare number. */
  amountRupees: string;
  currency: string;
  /** docs/01 §1's announced repricing, when the plan row carries one. `null` means unchanged. */
  displayFromDec2026: string | null;
}

export async function fetchPlanSummary(): Promise<PlanSummary[]> {
  try {
    const response = await fetch(`${apiOrigin()}/api/v1/plans`, {
      next: { revalidate: REVALIDATE_SECONDS, tags: ["plans"] },
    });
    if (!response.ok) return [];
    const payload = (await response.json()) as PlanListOut;
    return payload.data.map((plan) => ({
      code: plan.code,
      name: plan.label,
      display: formatPrice(plan.price_inr),
      cadence: billingCadence(plan.interval),
      amountRupees: plan.price_inr,
      currency: plan.currency,
      displayFromDec2026: plan.price_from_dec_2026
        ? formatPrice(plan.price_from_dec_2026)
        : null,
    }));
  } catch {
    return [];
  }
}
