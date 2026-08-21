import type { PlanOut } from "@baskfy/api-client";

/**
 * Presentation of a plan, and nothing else.
 *
 * Every number and every sentence here arrives from `GET /plans`; this module decides only how to
 * *show* them (PROMPTS.md Prompt 13 acceptance criterion 4). `price_inr` is a string on the wire
 * because docs/04 stores `numeric` and CLAUDE.md house rule 9 keeps money exact — it is parsed
 * once, here, for formatting only.
 */

const RUPEES = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  maximumFractionDigits: 0,
});

/**
 * The API's exact decimal string, rendered in rupees at whole-rupee precision — every plan price
 * in docs/01 §1 is a whole number. The amount itself is never written here.
 */
export function formatPrice(amount: string): string {
  const value = Number(amount);
  return Number.isFinite(value) ? RUPEES.format(value) : amount;
}

/** What follows the price on the card: `/mo`, `/yr`, or nothing for a one-time purchase. */
export function billingSuffix(interval: PlanOut["interval"]): string {
  if (interval === "month") return "/mo";
  if (interval === "year") return "/yr";
  return "one-time";
}

/** `month` → "Billed monthly". Used under the price, where the suffix alone is too terse. */
export function billingCadence(interval: PlanOut["interval"]): string {
  if (interval === "month") return "Billed every month. Cancel any time.";
  if (interval === "year") return "Billed once a year. Cancel any time.";
  return "A single payment. Nothing renews.";
}

/**
 * docs/01 §1 records a price increase in December 2026. The *amounts* are not written here — the
 * API serves them as `price_from_dec_2026`, and `no-hardcoded-pricing.test.ts` refuses a literal.
 */
export function repricingNote(plan: PlanOut): string | null {
  if (!plan.price_from_dec_2026) return null;
  return `Rising to ${formatPrice(plan.price_from_dec_2026)} in December 2026.`;
}
