"use client";

import {
  HOLDING_PROFILES,
  MAX_CASH_PCT,
  MAX_HOLDINGS,
  MIN_HOLDINGS,
  PROFILE_BLURBS,
  PROFILE_LABELS,
  suggestedHoldings,
  ZERO_CASH_PCT,
  type HoldingProfile,
} from "@/lib/basket/profiles";
import { cn } from "@/lib/utils";

/**
 * The two questions a screen does not answer: how much money, and across how many names (SB1).
 *
 * The profile *suggests* a count and the investor can always overrule it — which is why the
 * suggestion stays on screen next to the box rather than disappearing the moment it is edited.
 * "Reset to suggested" is how they get back, so overriding is never a one-way door.
 *
 * Cash is a third question (SB7): the system suggests a sleeve (5%, or more when the desk's
 * exposure tier says so). The investor can opt out entirely or type their own percent.
 */
export function SizingControls({
  amount,
  onAmountChange,
  profile,
  onProfileChange,
  holdings,
  onHoldingsChange,
  cashPct,
  onCashPctChange,
  suggestedCashPct,
  available,
  minimum,
  className,
}: {
  amount: number | null;
  onAmountChange: (value: number | null) => void;
  profile: HoldingProfile;
  onProfileChange: (value: HoldingProfile) => void;
  /** null means "follow the profile's suggestion". */
  holdings: number | null;
  onHoldingsChange: (value: number | null) => void;
  /** null = suggested sleeve; 0 = all into stocks; other = explicit percent kept as cash. */
  cashPct: number | null;
  onCashPctChange: (value: number | null) => void;
  suggestedCashPct: number;
  available: number;
  minimum: number;
  className?: string;
}) {
  const suggested = Math.min(suggestedHoldings(profile), available);
  const ceiling = Math.min(MAX_HOLDINGS, available);
  const effective = holdings ?? suggested;
  const useAllForStocks = cashPct === ZERO_CASH_PCT;
  const effectiveCashPct = cashPct ?? suggestedCashPct;
  const short = amount !== null && amount > 0 && amount < minimum;
  const amountHelp =
    amount === null || amount <= 0
      ? `Enter an amount — at least ₹${minimum.toLocaleString("en-IN")} for ${effective} names.`
      : short
        ? `₹${minimum.toLocaleString("en-IN")} is the least that fills all ${effective} names.`
        : `At least ₹${minimum.toLocaleString("en-IN")} for ${effective} names.`;

  return (
    <div className={cn("flex flex-col gap-4 rounded-xl border border-border/70 bg-card p-4", className)}>
      <div className="grid gap-4 sm:grid-cols-2">
        <label className="flex flex-col gap-1.5 text-sm">
          <span className="font-medium">Amount to invest</span>
          <div className="flex items-center gap-2 rounded-md border border-border bg-background px-3 focus-within:ring-2 focus-within:ring-ring">
            <span className="text-muted-foreground">₹</span>
            <input
              type="number"
              inputMode="numeric"
              min={0}
              step={1000}
              value={amount ?? ""}
              placeholder="Enter amount"
              onChange={(event) => {
                const raw = event.target.value;
                if (raw === "") {
                  onAmountChange(null);
                  return;
                }
                onAmountChange(Math.max(0, Number(raw) || 0));
              }}
              aria-label="Amount to invest"
              aria-describedby="sizing-amount-help"
              className="w-full bg-transparent py-2 tabular-nums outline-none"
            />
          </div>
          <span
            id="sizing-amount-help"
            className={cn("text-xs", short ? "text-warning-foreground" : "text-muted-foreground")}
          >
            {amountHelp}
          </span>
        </label>

        <label className="flex flex-col gap-1.5 text-sm">
          <span className="font-medium">Number of stocks</span>
          <input
            type="number"
            inputMode="numeric"
            min={MIN_HOLDINGS}
            max={ceiling}
            value={effective}
            onChange={(event) => {
              const next = Number(event.target.value);
              onHoldingsChange(Number.isFinite(next) && next > 0 ? Math.trunc(next) : null);
            }}
            aria-label="Number of stocks"
            aria-describedby="sizing-holdings-help"
            className="rounded-md border border-border bg-background px-3 py-2 tabular-nums"
          />
          <span id="sizing-holdings-help" className="text-xs text-muted-foreground">
            {holdings === null || holdings === suggested ? (
              <>Suggested for {PROFILE_LABELS[profile].toLowerCase()}. You can change it.</>
            ) : (
              <>
                Yours, not the suggested {suggested}.{" "}
                <button
                  type="button"
                  onClick={() => onHoldingsChange(null)}
                  className="text-accent underline-offset-4 hover:underline"
                >
                  Reset to suggested
                </button>
              </>
            )}
          </span>
        </label>
      </div>

      <fieldset className="flex flex-col gap-2">
        <legend className="text-sm font-medium">How spread out?</legend>
        <div className="flex flex-wrap gap-2" role="radiogroup" aria-label="Holding profile">
          {HOLDING_PROFILES.map((option) => (
            <button
              key={option}
              type="button"
              role="radio"
              aria-checked={profile === option}
              data-testid={`profile-${option.toLowerCase()}`}
              onClick={() => {
                onProfileChange(option);
                onHoldingsChange(null);
              }}
              className={cn(
                "rounded-lg border px-3 py-2 text-left text-sm transition-colors",
                profile === option
                  ? "border-transparent marker-control font-medium"
                  : "border-border/70 text-muted-foreground hover:bg-muted/50 hover:text-foreground",
              )}
            >
              <span className="block">{PROFILE_LABELS[option]}</span>
              <span className="block text-xs opacity-80">
                {Math.min(suggestedHoldings(option), available)} stocks
              </span>
            </button>
          ))}
        </div>
        <p className="text-xs text-muted-foreground">{PROFILE_BLURBS[profile]}</p>
      </fieldset>

      <fieldset className="flex flex-col gap-2">
        <legend className="text-sm font-medium">Cash</legend>
        <div className="flex flex-col gap-2" role="radiogroup" aria-label="Cash allocation">
          <label htmlFor="cash-sleeve-keep" className="flex cursor-pointer items-start gap-2 text-sm">
            <input
              id="cash-sleeve-keep"
              type="radio"
              name="cash-sleeve"
              data-testid="cash-sleeve-keep"
              checked={!useAllForStocks}
              onChange={() => onCashPctChange(null)}
              className="mt-1"
            />
            <span>
              <span className="font-medium">Keep a cash allocation</span>
              <span className="mt-1 block text-xs text-muted-foreground">
                Suggested {suggestedCashPct}%. You can change it.
              </span>
            </span>
          </label>
          {!useAllForStocks ? (
            <label className="ml-6 flex max-w-xs flex-col gap-1 text-sm">
              <span className="text-muted-foreground">Kept as cash (%)</span>
              <input
                type="number"
                inputMode="numeric"
                min={0}
                max={MAX_CASH_PCT}
                step={1}
                value={cashPct ?? ""}
                placeholder={String(suggestedCashPct)}
                onChange={(event) => {
                  const raw = event.target.value;
                  if (raw === "") {
                    onCashPctChange(null);
                    return;
                  }
                  onCashPctChange(
                    Math.min(MAX_CASH_PCT, Math.max(0, Math.trunc(Number(raw) || 0))),
                  );
                }}
                aria-label="Kept as cash percent"
                data-testid="cash-pct-input"
                className="rounded-md border border-border bg-background px-3 py-2 tabular-nums"
              />
              {cashPct !== null && cashPct !== suggestedCashPct ? (
                <span className="text-xs text-muted-foreground">
                  Yours, not the suggested {suggestedCashPct}%.{" "}
                  <button
                    type="button"
                    onClick={() => onCashPctChange(null)}
                    className="text-accent underline-offset-4 hover:underline"
                  >
                    Reset to suggested
                  </button>
                </span>
              ) : null}
            </label>
          ) : null}
          <label htmlFor="cash-sleeve-none" className="flex cursor-pointer items-start gap-2 text-sm">
            <input
              id="cash-sleeve-none"
              type="radio"
              name="cash-sleeve"
              data-testid="cash-sleeve-none"
              checked={useAllForStocks}
              onChange={() => onCashPctChange(ZERO_CASH_PCT)}
              className="mt-1"
            />
            <span>
              <span className="font-medium">Use all of it for stocks</span>
              <span className="mt-1 block text-xs text-muted-foreground">
                No cash allocation — only the small rounding remainder stays uninvested.
              </span>
            </span>
          </label>
        </div>
        {!useAllForStocks ? (
          <p className="text-xs text-muted-foreground">
            Preview uses {effectiveCashPct}% kept as cash until you save.
          </p>
        ) : null}
      </fieldset>
    </div>
  );
}
