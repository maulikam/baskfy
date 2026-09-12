"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

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
import { accessToken } from "@/lib/api/browser";
import { apiOrigin } from "@/lib/api/config";
import { formatNumber } from "@/lib/format";

type Step = "connect" | "import" | "preferences" | "pick";

/**
 * First-login onboarding: connect → import → save preferences → pick a basket (AF I.4).
 *
 * Goal-composer choices are PUT to `/watchlist/discover-preferences` so they live on the
 * account, not only in the Discover URL.
 */

export function OnboardingWizard({
  brokerConnected,
  holdingsSynced,
  sampleBaskets,
}: {
  brokerConnected: boolean;
  holdingsSynced: boolean;
  sampleBaskets: { slug: string; name: string }[];
}) {
  const router = useRouter();
  const [step, setStep] = useState<Step>(
    brokerConnected ? (holdingsSynced ? "preferences" : "import") : "connect",
  );
  const [prefs, setPrefs] = useState<Preferences>(DEFAULT_PREFERENCES);
  const [amountText, setAmountText] = useState(
    formatNumber(DEFAULT_PREFERENCES.amount, { decimals: 0 }),
  );
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function savePreferences(complete: boolean) {
    setSaving(true);
    setError(null);
    const amount = validAmount(amountText);
    const next = { ...prefs, amount };
    setPrefs(next);
    try {
      const token = await accessToken();
      const response = await fetch(`${apiOrigin()}/api/v1/watchlist/discover-preferences`, {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({
          goal: next.goal,
          horizon: next.horizon,
          risk: next.risk,
          amount: String(next.amount),
          rebalance: next.rebalance,
          onboarding_completed: complete,
        }),
      });
      if (!response.ok) {
        throw new Error(`Could not save preferences (${response.status})`);
      }
      if (complete) {
        router.push(`/discover?${preferencesToQuery(next)}`);
      } else {
        setStep("pick");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="flex max-w-xl flex-col gap-6" data-testid="onboarding-wizard">
      <ol className="flex flex-wrap gap-2 text-xs" aria-label="Onboarding steps">
        {(
          [
            ["connect", "Connect"],
            ["import", "Import"],
            ["preferences", "Preferences"],
            ["pick", "Pick a basket"],
          ] as const
        ).map(([id, label]) => (
          <li
            key={id}
            className={
              step === id
                ? "rounded-md border border-accent bg-accent-muted px-2 py-1 text-accent"
                : "rounded-md border border-border/70 px-2 py-1 text-muted-foreground"
            }
          >
            {label}
          </li>
        ))}
      </ol>

      {step === "connect" ? (
        <section className="space-y-3 rounded-xl border border-border/70 bg-card p-5">
          <h2 className="text-lg font-semibold">Connect a broker</h2>
          <p className="text-sm text-muted-foreground">
            Zerodha is live today. Connecting lets Baskfy read holdings; it never places an order
            from this flow.
          </p>
          {brokerConnected ? (
            <button
              type="button"
              className="text-sm text-accent underline-offset-4 hover:underline"
              onClick={() => setStep("import")}
            >
              Already connected — continue
            </button>
          ) : (
            <Link
              href="/brokers"
              className="inline-flex text-sm text-accent underline-offset-4 hover:underline"
            >
              Open Brokers
            </Link>
          )}
          <button
            type="button"
            className="block text-xs text-muted-foreground underline-offset-4 hover:underline"
            onClick={() => setStep("preferences")}
          >
            Skip for now
          </button>
        </section>
      ) : null}

      {step === "import" ? (
        <section className="space-y-3 rounded-xl border border-border/70 bg-card p-5">
          <h2 className="text-lg font-semibold">Import holdings</h2>
          <p className="text-sm text-muted-foreground">
            Sync once from the connected broker, or upload a holdings CSV on Brokers. Activity and
            realised P&amp;L still wait on the ledger (see BACKLOG).
          </p>
          <Link
            href="/brokers"
            className="inline-flex text-sm text-accent underline-offset-4 hover:underline"
          >
            Sync or upload
          </Link>
          <button
            type="button"
            className="block text-sm text-accent underline-offset-4 hover:underline"
            onClick={() => setStep("preferences")}
          >
            {holdingsSynced ? "Holdings look synced — continue" : "Continue without sync"}
          </button>
        </section>
      ) : null}

      {step === "preferences" ? (
        <section className="space-y-4 rounded-xl border border-border/70 bg-card p-5">
          <h2 className="text-lg font-semibold">How you want to invest</h2>
          <p className="text-sm text-muted-foreground">
            Saved to your account. Discover still uses the URL when you share a filter.
          </p>
          <div className="flex flex-wrap gap-3 text-sm">
            <SelectField
              label="Goal"
              value={prefs.goal}
              options={GOAL_COPY}
              onChange={(goal) => setPrefs({ ...prefs, goal: goal as GoalKey })}
            />
            <SelectField
              label="Horizon"
              value={prefs.horizon}
              options={HORIZON_COPY}
              onChange={(horizon) => setPrefs({ ...prefs, horizon: horizon as HorizonKey })}
            />
            <SelectField
              label="Risk"
              value={prefs.risk}
              options={RISK_COPY}
              onChange={(risk) => setPrefs({ ...prefs, risk: risk as RiskKey })}
            />
            <SelectField
              label="Rebalance"
              value={prefs.rebalance}
              options={REBALANCE_COPY}
              onChange={(rebalance) =>
                setPrefs({ ...prefs, rebalance: rebalance as RebalanceKey })
              }
            />
            <label className="text-sm">
              <span className="mb-1 block text-xs text-muted-foreground">Amount (INR)</span>
              <input
                value={amountText}
                onChange={(event) => setAmountText(event.target.value)}
                className="rounded-md border border-border bg-background px-2 py-1.5"
              />
            </label>
          </div>
          {error ? <p className="text-sm text-destructive">{error}</p> : null}
          <button
            type="button"
            disabled={saving}
            onClick={() => void savePreferences(false)}
            className="rounded-md bg-accent px-3 py-2 text-sm font-medium text-accent-foreground disabled:opacity-60"
            data-testid="onboarding-save-prefs"
          >
            {saving ? "Saving…" : "Save and pick a basket"}
          </button>
        </section>
      ) : null}

      {step === "pick" ? (
        <section className="space-y-3 rounded-xl border border-border/70 bg-card p-5">
          <h2 className="text-lg font-semibold">Pick a basket to start with</h2>
          <ul className="space-y-2">
            {sampleBaskets.map((basket) => (
              <li key={basket.slug}>
                <Link
                  href={`/basket/${basket.slug}`}
                  className="text-sm font-semibold text-accent underline-offset-4 hover:underline"
                >
                  {basket.name}
                </Link>
              </li>
            ))}
          </ul>
          <button
            type="button"
            disabled={saving}
            onClick={() => void savePreferences(true)}
            className="text-sm text-muted-foreground underline-offset-4 hover:underline"
            data-testid="onboarding-finish"
          >
            Finish and open matching baskets
          </button>
        </section>
      ) : null}
    </div>
  );
}

function SelectField({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: Record<string, string>;
  onChange: (value: string) => void;
}) {
  return (
    <label className="text-sm">
      <span className="mb-1 block text-xs text-muted-foreground">{label}</span>
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="rounded-md border border-border bg-background px-2 py-1.5"
      >
        {Object.entries(options).map(([key, copy]) => (
          <option key={key} value={key}>
            {copy}
          </option>
        ))}
      </select>
    </label>
  );
}
