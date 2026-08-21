"use client";

import { useState, useTransition } from "react";

import { startCheckout } from "@/app/actions/billing";
import { loadRazorpay, widgetOptions } from "@/components/billing/razorpay";
import { Button } from "@/components/ui/button";

/**
 * The one interactive element on `/pricing`.
 *
 * It knows the plan's *code* and nothing else — no price, no entitlement, no gateway id. Those
 * come back from `POST /checkout/session`, which reads them from the plan row (PROMPTS.md
 * Prompt 13 acceptance criterion 4).
 *
 * A signed-out visitor is sent to sign in first, with `?next=/pricing`, rather than being shown a
 * button that fails.
 */
export interface CheckoutButtonProps {
  planCode: string;
  label: string;
  signedIn: boolean;
  account: { email: string; name: string | null } | null;
  current: boolean;
}

export function CheckoutButton({
  planCode,
  label,
  signedIn,
  account,
  current,
}: CheckoutButtonProps) {
  const [pending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);

  if (current) {
    return (
      <Button variant="outline" disabled aria-disabled="true" className="w-full">
        Your current plan
      </Button>
    );
  }

  if (!signedIn || !account) {
    return (
      <Button asChild className="w-full">
        <a href={`/login?next=${encodeURIComponent("/pricing")}`}>Sign in to choose {label}</a>
      </Button>
    );
  }

  const onClick = () => {
    setError(null);
    startTransition(async () => {
      const result = await startCheckout(planCode);
      if (!result.ok) {
        setError(result.message);
        return;
      }
      try {
        const Razorpay = await loadRazorpay();
        new Razorpay(widgetOptions(result.session, account)).open();
      } catch {
        setError("The payment window could not be opened. Try again, or use another browser.");
      }
    });
  };

  return (
    <div className="flex flex-col gap-2">
      <Button onClick={onClick} disabled={pending} className="w-full">
        {pending ? "Opening…" : `Choose ${label}`}
      </Button>
      {error ? (
        <p role="alert" className="text-xs text-negative">
          {error}
        </p>
      ) : null}
    </div>
  );
}
