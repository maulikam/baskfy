"use client";

import { useId, useState } from "react";

import { KiteBasketForm, type KiteBasketItem } from "@/components/cb/kite-basket-form";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { browserApi } from "@/lib/api/browser";
import { formatNumber } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * "How much?" → a basket you confirm in Kite.
 *
 * A curated basket stores **weights**, not quantities: "12% RELIANCE" is a proportion, and a
 * proportion cannot be sent to a broker. Turning it into "buy 5 shares" needs one thing only the
 * person reading the page knows — how much money they want to put in. So the amount is the whole
 * of this form, and everything after it is arithmetic.
 *
 * **The arithmetic is the server's**, deliberately. `GET /explore/{slug}/kite?amount=` runs
 * `build_invest_plan`, the same pure function `POST /cb/plans/invest` uses for the plan preview.
 * Doing it here in floating point would produce a basket that disagreed with that preview in
 * rupees, and the user would have no way to tell which was right.
 *
 * What comes back is already batched to Kite's ten-instrument limit and already carries the
 * publisher key, so this component never assembles an order — it renders what the API computed.
 */

/**
 * Whole rupees — paise in an amount field is a decision nobody wants to make about a basket.
 *
 * `step={1}`, and that is a correctness constraint rather than a UX preference. A nicer-feeling
 * `step={1000}` alongside `min={1}` makes the *only* valid amounts 1, 1001, 2001 … — every round
 * figure a person would actually type, ₹50,000 and ₹100,000 included, fails `stepMismatch` and
 * the browser silently refuses to submit the form. The control looks fine and the button does
 * nothing. A component test caught it; nobody clicking around would have known why.
 */
const AMOUNT_STEP = 1;
const DEFAULT_AMOUNT = 100_000;

interface KiteBasket {
  url: string;
  api_key: string;
  configured: boolean;
  items: KiteBasketItem[];
  batches?: KiteBasketItem[][];
  excluded?: string[];
}

export function KiteBasketInvest({
  basketSlug,
  basketName,
  minAmount = null,
  className,
}: {
  basketSlug: string;
  basketName?: string;
  /** Catalog minimum — warn when the typed amount is below it (audit §1.2). */
  minAmount?: string | number | null;
  className?: string;
}) {
  const amountId = useId();
  const [amount, setAmount] = useState(String(DEFAULT_AMOUNT));
  const [basket, setBasket] = useState<KiteBasket | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  const minRupees =
    minAmount === null || minAmount === undefined || minAmount === ""
      ? null
      : Number(minAmount);
  const amountRupees = Number(amount);
  const underMin =
    minRupees !== null &&
    Number.isFinite(minRupees) &&
    Number.isFinite(amountRupees) &&
    amountRupees > 0 &&
    amountRupees < minRupees;

  /* `void`-returning, with the promise handled inside. An `async` handler passed straight to
     `onSubmit` returns a promise React never awaits, so a rejection becomes an unhandled one —
     the lint rule is right, and the fix is to own the boundary rather than silence it. */
  function build(event: React.FormEvent): void {
    event.preventDefault();
    void run();
  }

  async function run(): Promise<void> {
    const rupees = Number(amount);
    if (!Number.isFinite(rupees) || rupees <= 0) {
      setError("Enter an amount in rupees.");
      return;
    }
    setPending(true);
    setError(null);
    /* The previous basket is cleared before the request, not after it. Leaving it on screen while
       a new amount is in flight shows share counts that belong to the old number — and the button
       under them would post exactly those. */
    setBasket(null);
    try {
      const { data, error: failed } = await browserApi().GET("/api/v1/explore/{slug}/kite", {
        params: { path: { slug: basketSlug }, query: { amount: String(rupees) } },
      });
      if (failed || !data) {
        setError(
          (failed as { detail?: string } | undefined)?.detail ??
            "That basket could not be priced right now.",
        );
        return;
      }
      setBasket(data);
    } catch {
      setError("That basket could not be priced right now.");
    } finally {
      setPending(false);
    }
  }

  return (
    <div
      className={cn(
        "flex min-w-0 max-w-full flex-col gap-3 overflow-x-hidden rounded-xl border border-border bg-card p-4",
        className,
      )}
    >
      <div className="min-w-0 space-y-1">
        <h2 className="text-sm font-semibold text-foreground">
          Trade {basketName ?? "this basket"} in Kite
        </h2>
        <p className="break-words text-sm leading-relaxed text-muted-foreground">
          Choose an amount and we work out the share counts. The basket opens in your own Zerodha
          account, where you review every line and confirm — nothing is placed from here.
        </p>
      </div>

      <form onSubmit={build} className="flex min-w-0 flex-wrap items-end gap-2">
        <div className="flex min-w-0 flex-col gap-1.5">
          <Label htmlFor={amountId}>Amount to invest (₹)</Label>
          <Input
            id={amountId}
            name="amount"
            type="number"
            inputMode="numeric"
            min={1}
            step={AMOUNT_STEP}
            value={amount}
            onChange={(event) => setAmount(event.currentTarget.value)}
            className="tnum w-40 max-w-full"
          />
        </div>
        <Button type="submit" variant="outline" size="sm" disabled={pending}>
          {pending ? "Working it out…" : basket ? "Recalculate" : "Work out the shares"}
        </Button>
      </form>

      {underMin && minRupees !== null ? (
        <p role="status" className="break-words text-sm text-muted-foreground">
          This amount is below the catalog minimum of ₹{formatNumber(minRupees, { decimals: 0 })}.
          Some names will buy zero shares until you raise it.
        </p>
      ) : null}

      <p role="status" aria-live="polite" className="min-h-5 text-sm text-negative">
        {error ?? ""}
      </p>

      {basket ? (
        <>
          <KiteBasketForm
            url={basket.url}
            apiKey={basket.api_key}
            configured={basket.configured}
            items={basket.items}
            batches={basket.batches ?? []}
            zeroShareDrops={basket.excluded ?? []}
          />
          <p className="break-words text-sm leading-relaxed text-muted-foreground">
            Share counts are whole numbers, so the basket comes to a little under your amount. Kite
            shows live prices before you confirm; these are worked out from the last close.
          </p>
        </>
      ) : null}
    </div>
  );
}
