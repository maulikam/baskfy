import "server-only";

import type { InvoicePage, PlanListOut } from "@decile/api-client";

import { apiOrigin } from "@/lib/api/config";
import { auth } from "@/lib/auth";

/**
 * Server-side reads for the billing surfaces — docs/07 §"Account & billing" (Prompt 13).
 *
 * PROMPTS.md Prompt 13 acceptance criterion 4:
 *
 *     "No price or entitlement is hard-coded in the web app; all read from the API."
 *
 * So `/pricing` renders `GET /plans` — prices, the Dec-2026 prices, the per-plan feature list, the
 * Forever disclosure and the pre-purchase disclaimers all come down the wire. There is no plan
 * table in this repository's TypeScript, and `src/lib/__tests__/no-hardcoded-pricing.test.ts`
 * scans for one.
 *
 * `/plans` is cached briefly: it is public, identical for everyone, and changes when an operator
 * edits a plan row rather than on any request. `/invoices` is never cached — it is per-user, and a
 * cached invoice list is the classic way to show one account's billing to another.
 */

/** Long enough to survive a burst on the pricing page, short enough that a repricing lands soon. */
const PLANS_REVALIDATE_SECONDS = 300;

export class BillingUnavailable extends Error {}

export async function fetchPlans(): Promise<PlanListOut> {
  const response = await fetch(`${apiOrigin()}/api/v1/plans`, {
    next: { revalidate: PLANS_REVALIDATE_SECONDS, tags: ["plans"] },
  });
  if (!response.ok) throw new BillingUnavailable(`/plans responded ${response.status}`);
  return (await response.json()) as PlanListOut;
}

/** `null` when there is no session — the caller redirects rather than rendering an empty list. */
export async function fetchInvoices(): Promise<InvoicePage | null> {
  const session = await auth();
  const token = session?.accessToken;
  if (!token) return null;

  const response = await fetch(`${apiOrigin()}/api/v1/invoices`, {
    headers: { Authorization: `Bearer ${token}` },
    cache: "no-store",
  });
  if (!response.ok) return null;
  return (await response.json()) as InvoicePage;
}
