"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  MarkInvestedError,
  markInvested,
  parseHoldingLines,
} from "@/lib/investments/mark";

/**
 * T8.1 — the user confirms they already invested at the broker.
 * Submits POST /cb/investments/mark. Never a desk execute endpoint.
 */

export function MarkInvestedForm({
  basketSlug,
  basketName,
}: {
  basketSlug: string;
  basketName: string;
}) {
  const router = useRouter();
  const [amount, setAmount] = useState("");
  const [holdingsText, setHoldingsText] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    if (!confirmed) {
      setError("Tick the box only after you have already placed the orders at your broker.");
      return;
    }
    const rupees = Number(amount);
    if (!Number.isFinite(rupees) || rupees <= 0) {
      setError("Enter the amount you invested, in rupees.");
      return;
    }
    let holdings;
    try {
      holdings = parseHoldingLines(holdingsText);
    } catch (caught) {
      setError(caught instanceof MarkInvestedError ? caught.message : "Could not read holdings.");
      return;
    }
    if (holdings.length < 1) {
      setError("List at least one holding: SYMBOL QTY AVG_PRICE, one per line.");
      return;
    }
    setBusy(true);
    try {
      const result = await markInvested({
        basket_slug: basketSlug,
        amount: rupees,
        confirmed: true,
        holdings,
      });
      router.push(`/me/investments/${result.id}`);
      router.refresh();
    } catch (caught) {
      setError(
        caught instanceof MarkInvestedError
          ? caught.message
          : "Could not record the investment.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      data-testid="mark-invested-form"
      onSubmit={onSubmit}
      className="flex flex-col gap-3 rounded-xl border border-border bg-card p-4"
    >
      <div className="space-y-1">
        <h3 className="text-sm font-semibold text-foreground">I invested this at my broker</h3>
        <p className="text-sm leading-relaxed text-muted-foreground">
          Records {basketName} on Baskfy after you have already traded. This form cannot place
          an order.
        </p>
      </div>
      <div className="space-y-1">
        <Label htmlFor="mark-amount">Amount invested (₹)</Label>
        <Input
          id="mark-amount"
          inputMode="decimal"
          value={amount}
          onChange={(event) => setAmount(event.target.value)}
          placeholder="203374"
        />
      </div>
      <div className="space-y-1">
        <Label htmlFor="mark-holdings">Holdings — one per line: SYMBOL QTY AVG_PRICE</Label>
        <textarea
          id="mark-holdings"
          data-testid="mark-holdings"
          value={holdingsText}
          onChange={(event) => setHoldingsText(event.target.value)}
          placeholder={"CUPID 10 81.80"}
          rows={4}
          className="w-full rounded-md border border-input bg-card px-3 py-2 text-sm"
        />
      </div>
      <label className="flex items-start gap-2 text-sm text-foreground">
        <input
          type="checkbox"
          data-testid="mark-confirmed"
          checked={confirmed}
          onChange={(event) => setConfirmed(event.target.checked)}
          className="mt-0.5"
        />
        I have already placed these orders at my broker. Baskfy is recording the book, not
        sending them.
      </label>
      {error ? (
        <p className="text-sm text-negative" role="alert">
          {error}
        </p>
      ) : null}
      <Button type="submit" size="sm" disabled={busy} data-testid="mark-invested-submit">
        {busy ? "Recording…" : "Mark as invested"}
      </Button>
    </form>
  );
}
