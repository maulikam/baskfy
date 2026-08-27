"use client";

import { useRouter } from "next/navigation";
import { useId, useState } from "react";

import {
  DEFAULT_PREFERENCES,
  GOAL_COPY,
  HORIZON_COPY,
  type GoalKey,
  type HorizonKey,
  type Preferences,
  REBALANCE_COPY,
  RISK_COPY,
  type RebalanceKey,
  type RiskKey,
} from "@/lib/discover/match";
import { preferencesToQuery, validAmount } from "@/lib/discover/preferences";
import { cn } from "@/lib/utils";

/**
 * "I want long-term growth over 5+ years, with moderate volatility, investing around ₹5 lakh."
 *
 * A sentence rather than a form, because the reader is describing themselves and a sentence is
 * how people describe themselves. Five `<select>`s in a row with labels above them ask the same
 * questions and read like a tax return.
 *
 * **Native controls, deliberately.** Each field is a real `<select>` or `<input>` styled to sit in
 * the line of text. They are keyboard-navigable, screen-reader-labelled and work on a phone's
 * native picker without a line of our own code; a custom dropdown would be prettier in a
 * screenshot and worse everywhere else.
 *
 * **The button says what it does.** "Show matching baskets", not "Find your basket" — the D3
 * regulatory posture is unreviewed and this product is not an adviser, so the control performs a
 * filter and its label says so.
 */
export function GoalComposer({
  initial = DEFAULT_PREFERENCES,
  className,
}: {
  initial?: Preferences;
  className?: string;
}) {
  const router = useRouter();
  const [prefs, setPrefs] = useState<Preferences>(initial);
  const [amountText, setAmountText] = useState(String(initial.amount));
  const amountId = useId();

  function submit(event: React.FormEvent) {
    event.preventDefault();
    // The same validator the URL uses, so typing 5,00,000 and linking to ?amount=500000 agree —
    // and so a negative amount cannot slip past by having its sign stripped.
    const amount = validAmount(amountText);
    const next = { ...prefs, amount };
    setPrefs(next);
    setAmountText(String(amount));
    router.push(`/discover?${preferencesToQuery(next)}`);
  }

  return (
    <form
      onSubmit={submit}
      className={cn("space-y-4", className)}
      data-testid="goal-composer"
      aria-labelledby={`${amountId}-heading`}
    >
      <h2 id={`${amountId}-heading`} className="text-xl font-semibold tracking-tight sm:text-2xl">
        Find a basket for the way you invest
      </h2>

      <p className="flex flex-wrap items-baseline gap-x-1.5 gap-y-2 text-base leading-loose">
        <span>I want</span>
        <Field
          label="Investment goal"
          value={prefs.goal}
          options={GOAL_COPY}
          onChange={(goal) => setPrefs({ ...prefs, goal: goal as GoalKey })}
        />
        <span>over</span>
        <Field
          label="Investment horizon"
          value={prefs.horizon}
          options={HORIZON_COPY}
          onChange={(horizon) => setPrefs({ ...prefs, horizon: horizon as HorizonKey })}
        />
        <span>, with</span>
        <Field
          label="Risk preference"
          value={prefs.risk}
          options={RISK_COPY}
          onChange={(risk) => setPrefs({ ...prefs, risk: risk as RiskKey })}
        />
        <span>, investing around</span>
        <span className="inline-flex items-baseline">
          <label htmlFor={amountId} className="sr-only">
            Investment amount in rupees
          </label>
          <span aria-hidden="true" className="font-medium">
            ₹
          </span>
          <input
            id={amountId}
            name="amount"
            inputMode="numeric"
            value={amountText}
            onChange={(event) => setAmountText(event.target.value)}
            className="w-28 border-b border-dashed border-foreground/40 bg-transparent px-1 font-medium tabular-nums outline-none focus:border-solid focus:border-foreground"
          />
        </span>
        <span>, rebalanced</span>
        <Field
          label="Rebalance preference"
          value={prefs.rebalance}
          options={REBALANCE_COPY}
          onChange={(rebalance) =>
            setPrefs({ ...prefs, rebalance: rebalance as RebalanceKey })
          }
        />
        <span>.</span>
      </p>

      <div className="flex flex-wrap items-center gap-3">
        <button
          type="submit"
          data-testid="show-matching"
          className="rounded-md bg-accent px-4 py-2 text-sm font-medium text-accent-foreground transition-opacity hover:opacity-90"
        >
          Show matching baskets
        </button>
        <p className="max-w-[62ch] text-xs leading-relaxed text-muted-foreground">
          This filters the catalogue against what you just said. Baskfy is not a registered
          adviser and does not know your circumstances, so nothing here is a recommendation —
          each result shows which of your preferences it did and did not match.
        </p>
      </div>
    </form>
  );
}

function Field<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: T;
  options: Record<T, string>;
  onChange: (value: string) => void;
}) {
  const id = useId();
  return (
    <span className="inline-flex items-baseline">
      <label htmlFor={id} className="sr-only">
        {label}
      </label>
      <select
        id={id}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        data-testid={`field-${label.toLowerCase().replace(/\s+/g, "-")}`}
        className="max-w-[16rem] cursor-pointer appearance-none border-b border-dashed border-foreground/40 bg-transparent px-1 font-medium outline-none focus:border-solid focus:border-foreground"
      >
        {(Object.entries(options) as [T, string][]).map(([key, copy]) => (
          <option key={key} value={key}>
            {copy}
          </option>
        ))}
      </select>
      <span aria-hidden="true" className="-ml-0.5 text-[10px] text-muted-foreground">
        ▾
      </span>
    </span>
  );
}
