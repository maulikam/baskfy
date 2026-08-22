import type { Metadata } from "next";

import { PlanCard } from "@/components/billing/plan-card";
import { Disclaimer } from "@/components/data/disclaimer";
import { fetchPlans } from "@/lib/billing/fetch";
import { fetchMe } from "@/lib/auth/me";

/**
 * `/pricing` — docs/01 §1, PROMPTS.md Prompt 13 §5.
 *
 *     "/pricing page mirroring the reference's structure including the 'Forever means the lifetime
 *      of the website' clarification and the pre-purchase disclaimer bullets."
 *
 * Everything on this page comes from `GET /plans`: the three prices, the December-2026 prices, the
 * per-plan feature list, the Forever disclosure and the disclaimer bullets. Nothing about money is
 * written in this repository's TypeScript — that is Prompt 13's fourth acceptance criterion, and
 * `src/lib/__tests__/no-hardcoded-pricing.test.ts` is what keeps it true.
 *
 * docs/11 §Compliance requires the `<Disclaimer/>` "on checkout"; this is the checkout surface.
 */
export const metadata: Metadata = {
  title: "Pricing",
  description:
    "Baskfy plans: the full NSE momentum screener, CSV export, custom columns, historical ranks " +
    "and backtests. Monthly, yearly, or a one-time Forever purchase.",
};

export default async function PricingPage() {
  const [plans, me] = await Promise.all([fetchPlans(), fetchMe()]);
  const account = me ? { email: me.email, name: me.name ?? null } : null;
  const entitled = me?.subscription_status === "active" ? (me.plan_code ?? null) : null;

  return (
    <div className="flex flex-col gap-8">
      <header className="flex max-w-2xl flex-col gap-2">
        <h1 className="text-2xl font-semibold tracking-tight">Pricing</h1>
        <p className="text-sm text-muted-foreground">
          One product, three ways to pay for it. Every plan includes the same features; they differ
          only in how long they last.
        </p>
      </header>

      <section aria-label="Plans" className="grid gap-4 md:grid-cols-3">
        {plans.data.map((plan) => (
          <PlanCard
            key={plan.code}
            plan={plan}
            featured={plan.interval === "year"}
            signedIn={me !== null}
            account={account}
            currentPlanCode={entitled}
          />
        ))}
      </section>

      <section aria-labelledby="before-you-buy" className="flex max-w-2xl flex-col gap-3">
        <h2 id="before-you-buy" className="text-sm font-semibold">
          Before you buy
        </h2>
        <ul className="flex list-disc flex-col gap-2 pl-5 text-sm text-muted-foreground">
          {plans.disclaimers.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      </section>

      {/*
        * A second, prominent disclaimer, above the app shell's own. docs/11 §Compliance lists
        * three places it must appear — "on every analytics surface, in the footer, and **on
        * checkout**" — and this is the checkout surface; the shell's footer copy is the footer.
        */}
      <Disclaimer variant="block" className="max-w-2xl" />
    </div>
  );
}
