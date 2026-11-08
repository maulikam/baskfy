import { Check } from "lucide-react";
import type { PlanOut } from "@baskfy/api-client";

import { CheckoutButton } from "@/components/billing/checkout-button";
import { Badge } from "@/components/ui/badge";
import { billingCadence, billingSuffix, formatPrice, repricingNote } from "@/lib/billing/format";

/**
 * One plan, rendered entirely from `GET /plans`.
 *
 * There is no price, no feature list and no entitlement written into this file — every one of them
 * is a field on `plan` (PROMPTS.md Prompt 13 acceptance criterion 4, and
 * `src/lib/__tests__/no-hardcoded-pricing.test.ts`, which scans for a literal that looks like one).
 */
export interface PlanCardProps {
  plan: PlanOut;
  featured: boolean;
  signedIn: boolean;
  account: { email: string; name: string | null } | null;
  currentPlanCode: string | null;
}

export function PlanCard({ plan, featured, signedIn, account, currentPlanCode }: PlanCardProps) {
  return (
    <article
      aria-labelledby={`plan-${plan.code}`}
      className={[
        "flex flex-col gap-4 rounded-lg border bg-card p-5",
        featured ? "border-accent shadow-sm" : "border-border",
      ].join(" ")}
    >
      <header className="flex flex-col gap-1.5">
        <div className="flex items-center justify-between gap-2">
          <h2 id={`plan-${plan.code}`} className="text-base font-semibold">
            {plan.label}
          </h2>
          {featured ? <Badge variant="positive">Best value</Badge> : null}
        </div>
        <p className="text-sm text-muted-foreground">{plan.tagline}</p>
      </header>

      <p className="flex items-baseline gap-1">
        <span className="text-3xl font-semibold tabular-nums tracking-tight">
          {formatPrice(plan.price_inr)}
        </span>
        <span className="text-sm text-muted-foreground">{billingSuffix(plan.interval)}</span>
      </p>
      <p className="-mt-3 text-xs text-muted-foreground">{billingCadence(plan.interval)}</p>

      <ul className="flex flex-col gap-1.5 text-sm">
        {plan.features.map((feature) => (
          <li key={feature.label} className="flex items-start gap-2">
            <Check aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-positive" />
            <span>{feature.label}</span>
          </li>
        ))}
      </ul>

      {plan.disclosure ? (
        <p className="rounded-md border border-border bg-muted/50 p-3 text-xs leading-relaxed">
          {plan.disclosure}
        </p>
      ) : null}

      <div className="mt-auto flex flex-col gap-2">
        <CheckoutButton
          planCode={plan.code}
          label={plan.label}
          signedIn={signedIn}
          account={account}
          current={currentPlanCode === plan.code}
        />
        {repricingNote(plan) ? (
          <p className="text-xs text-muted-foreground">{repricingNote(plan)}</p>
        ) : null}
      </div>
    </article>
  );
}
