import type { CheckoutSessionOut } from "@baskfy/api-client";

/**
 * Razorpay's browser widget, typed rather than reached for through `any`.
 *
 * `apps/web` has zero `any`, enforced by a source scan (`src/lib/__tests__/no-any.test.ts`), so
 * the two things this file touches on `window` are declared instead of asserted.
 *
 * **The script is loaded on click, never on page load.** `/pricing` is a public marketing page
 * that must render — and must be measurable by Lighthouse — without a third-party request. It is
 * also why nothing in the test suite ever contacts Razorpay: no click, no script.
 */

const SCRIPT_URL = "https://checkout.razorpay.com/v1/checkout.js";
const SCRIPT_ID = "razorpay-checkout";

export interface RazorpayOptions {
  key: string;
  name: string;
  description: string;
  order_id?: string;
  subscription_id?: string;
  amount?: number;
  currency?: string;
  prefill?: { email?: string; name?: string };
  notes?: Record<string, string>;
}

interface RazorpayInstance {
  open: () => void;
}

interface RazorpayConstructor {
  new (options: RazorpayOptions): RazorpayInstance;
}

declare global {
  interface Window {
    Razorpay?: RazorpayConstructor;
  }
}

export class CheckoutUnavailable extends Error {}

/** Inject the widget's script once, and resolve when it is ready. */
export async function loadRazorpay(): Promise<RazorpayConstructor> {
  if (window.Razorpay) return window.Razorpay;

  await new Promise<void>((resolve, reject) => {
    const existing = document.getElementById(SCRIPT_ID);
    if (existing) {
      existing.addEventListener("load", () => resolve(), { once: true });
      existing.addEventListener(
        "error",
        () => reject(new CheckoutUnavailable("Razorpay's checkout script did not load.")),
        { once: true },
      );
      return;
    }
    const script = document.createElement("script");
    script.id = SCRIPT_ID;
    script.src = SCRIPT_URL;
    script.async = true;
    script.addEventListener("load", () => resolve(), { once: true });
    script.addEventListener(
      "error",
      () => reject(new CheckoutUnavailable("Razorpay's checkout script did not load.")),
      { once: true },
    );
    document.body.append(script);
  });

  const loaded = window.Razorpay;
  if (!loaded) throw new CheckoutUnavailable("Razorpay's checkout script did not load.");
  return loaded;
}

/**
 * Translate the API's session into the widget's options.
 *
 * Nothing is computed here: `key_id`, the amount and the gateway id are the API's, and a
 * subscription checkout carries `subscription_id` while a one-time purchase carries `order_id`.
 */
export function widgetOptions(
  session: CheckoutSessionOut,
  account: { email: string; name: string | null },
): RazorpayOptions {
  const amountPaise = Math.round(Number(session.amount_inr) * 100);
  const base: RazorpayOptions = {
    key: session.key_id,
    name: "Baskfy",
    description: `Baskfy ${session.plan_code} plan`,
    currency: session.currency,
    prefill: { email: account.email, ...(account.name ? { name: account.name } : {}) },
  };
  if (session.kind === "subscription" && session.subscription_id) {
    return { ...base, subscription_id: session.subscription_id };
  }
  return { ...base, amount: amountPaise, ...(session.order_id ? { order_id: session.order_id } : {}) };
}
