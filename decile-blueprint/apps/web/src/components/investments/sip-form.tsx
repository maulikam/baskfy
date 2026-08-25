"use client";

import { useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { SipError, createSipReminder } from "@/lib/investments/sip";

/**
 * T8.4 — persist a REMINDER SIP. No auto-debit.
 */

export function SipForm({ investmentId }: { investmentId: string }) {
  const [amount, setAmount] = useState("");
  const [day, setDay] = useState("21");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [nextFire, setNextFire] = useState<string | null>(null);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    const rupees = Number(amount);
    const dayOfMonth = Number(day);
    if (!Number.isFinite(rupees) || rupees <= 0) {
      setError("Enter the instalment amount in rupees.");
      return;
    }
    if (!Number.isInteger(dayOfMonth) || dayOfMonth < 1 || dayOfMonth > 28) {
      setError("Day of month must be 1–28.");
      return;
    }
    setBusy(true);
    try {
      const plan = await createSipReminder(investmentId, {
        amount: rupees,
        day_of_month: dayOfMonth,
      });
      setNextFire(String(plan.next_fire_date));
    } catch (caught) {
      setError(caught instanceof SipError ? caught.message : "Could not save the reminder.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      data-testid="sip-form"
      onSubmit={onSubmit}
      className="flex flex-col gap-3 rounded-xl border border-border bg-card p-4"
    >
      <div className="space-y-1">
        <h3 className="text-sm font-semibold text-foreground">SIP reminder</h3>
        <p className="text-sm leading-relaxed text-muted-foreground">
          Baskfy will remind you. It will not debit your bank or send broker instructions.
        </p>
      </div>
      <div className="space-y-1">
        <Label htmlFor="sip-amount">Instalment (₹)</Label>
        <Input
          id="sip-amount"
          inputMode="decimal"
          value={amount}
          onChange={(event) => setAmount(event.target.value)}
          placeholder="5000"
        />
      </div>
      <div className="space-y-1">
        <Label htmlFor="sip-day">Day of month (1–28)</Label>
        <Input
          id="sip-day"
          inputMode="numeric"
          value={day}
          onChange={(event) => setDay(event.target.value)}
        />
      </div>
      {error ? (
        <p className="text-sm text-negative" role="alert">
          {error}
        </p>
      ) : null}
      {nextFire ? (
        <p className="text-sm text-muted-foreground">Next reminder on {nextFire}.</p>
      ) : null}
      <Button type="submit" size="sm" disabled={busy} data-testid="sip-submit">
        {busy ? "Saving…" : "Save reminder"}
      </Button>
    </form>
  );
}
