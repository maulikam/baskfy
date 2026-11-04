"use client";

import { useEffect, useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  analyzeDeskBuy,
  confirmLabel,
  DeskBuyError,
  executeDeskBuy,
  fetchDeskStatus,
  lotQuantity,
  type CashProduct,
  type DeskAnalyze,
  type DeskExecute,
  type DeskStatus,
} from "@/lib/opportunities/desk-buy";
import { formatInr } from "@/lib/opportunities/view";
import { useLiveMarks } from "@/lib/screens/live-marks";
import type { SwingOpportunity } from "@/lib/opportunities/types";

function formatAmount(value: string): string {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return value;
  return Math.round(n).toLocaleString("en-IN");
}

export function SwingBuyButton({ opportunity }: { opportunity: SwingOpportunity }) {
  const live = useLiveMarks([opportunity.symbol]);
  const lastPrice =
    live.marks[opportunity.symbol] ??
    live.quotes[opportunity.symbol]?.lastPrice ??
    opportunity.close ??
    opportunity.entry.price;
  const trigger = opportunity.trigger.level || opportunity.entry.price;
  const stop = opportunity.invalidation;
  const disabled = !(opportunity.instrument_id > 0) || !(lastPrice > 0) || !(stop > 0) || stop >= trigger;

  return (
    <BuyDialog
      symbol={opportunity.symbol}
      instrumentId={opportunity.instrument_id}
      setup={opportunity.setup}
      lastPrice={lastPrice}
      trigger={trigger}
      stop={stop}
      lotSize={1}
      disabled={disabled}
      {...(disabled
        ? { disabledReason: "Need a last price, a trigger and an invalidation below it." }
        : {})}
    />
  );
}

function BuyDialog({
  symbol,
  instrumentId,
  setup,
  lastPrice,
  trigger,
  stop,
  lotSize,
  disabled,
  disabledReason,
}: {
  symbol: string;
  instrumentId: number;
  setup: string;
  lastPrice: number;
  trigger: number;
  stop: number;
  lotSize: number;
  disabled: boolean;
  disabledReason?: string;
}) {
  const [open, setOpen] = useState(false);
  const [product, setProduct] = useState<CashProduct>("CNC");
  const [amount, setAmount] = useState("");
  const [status, setStatus] = useState<DeskStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [plan, setPlan] = useState<DeskAnalyze | null>(null);
  const [busy, setBusy] = useState<"status" | "analyze" | "execute" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<DeskExecute | null>(null);

  useEffect(() => {
    if (!open) {
      setProduct("CNC");
      setAmount("");
      setPlan(null);
      setError(null);
      setOutcome(null);
      setStatusError(null);
      return;
    }
    let cancelled = false;
    setBusy("status");
    fetchDeskStatus()
      .then((s) => {
        if (!cancelled) setStatus(s);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setStatus({ authed: false, dry_run: true, cash: null });
          setStatusError(err instanceof DeskBuyError ? err.message : "Could not reach the desk.");
        }
      })
      .finally(() => {
        if (!cancelled) setBusy(null);
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  const qty = lotQuantity(Number(amount), lastPrice, lotSize);
  const notional = qty * lastPrice;
  // Don't claim LIVE until analyze says so — local DRY_RUN can be off while swing
  // execution is still gated, which would make a pre-review LIVE banner a lie.
  const simulated = plan?.simulated ?? true;
  const live = Boolean(plan) && !simulated;
  const kiteOk = plan?.kite_authed ?? status?.authed ?? false;

  const confirmText = useMemo(() => {
    const qtyShown = plan?.quantity ?? qty;
    const amt = plan?.amount_inr ?? amount;
    if (qtyShown <= 0 || !amt) return "";
    return confirmLabel(live, product, qtyShown, symbol, formatAmount(String(amt)));
  }, [plan, qty, amount, live, product, symbol]);

  async function onReview() {
    setError(null);
    setOutcome(null);
    setBusy("analyze");
    try {
      const next = await analyzeDeskBuy({
        symbol,
        instrument_id: instrumentId,
        setup,
        amount_inr: amount,
        product,
        last_price: String(lastPrice),
        trigger: String(trigger),
        stop: String(stop),
        lot_size: lotSize,
      });
      setPlan(next);
      if (next.skip) setError(next.skip);
    } catch (err: unknown) {
      setPlan(null);
      setError(err instanceof Error ? err.message : "Analyze failed.");
    } finally {
      setBusy(null);
    }
  }

  async function onConfirm() {
    if (!plan?.confirmable || !plan.plan_id || plan.line_id == null) return;
    setError(null);
    setBusy("execute");
    try {
      const result = await executeDeskBuy(plan.plan_id, plan.line_id);
      setOutcome(result);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Execute failed.");
    } finally {
      setBusy(null);
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={disabled}
          title={disabledReason}
          onClick={(event) => event.stopPropagation()}
        >
          Buy
        </Button>
      </DialogTrigger>
      <DialogContent className="max-w-md space-y-4" onClick={(event) => event.stopPropagation()}>
        <DialogTitle>Buy {symbol}</DialogTitle>
        <DialogDescription>
          Sizes a desk plan from the rupee amount, then a second click confirms. There is no auto-submit.
        </DialogDescription>

        {live ? (
          <p className="rounded-md border border-negative/40 bg-negative/10 px-3 py-2 text-sm" data-testid="buy-live-banner">
            Confirm places a <strong>real Zerodha order</strong>. DRY_RUN is off.
          </p>
        ) : (
          <p className="rounded-md border border-border bg-muted px-3 py-2 text-sm" data-testid="buy-sim-banner">
            This confirm will <strong>simulate</strong>
            {status?.dry_run ? " (DRY_RUN)" : ""}
            {plan && !plan.swing_execution_enabled ? " — swing execution is off on this stack" : ""}.
            No live order leaves the gateway.
          </p>
        )}

        {statusError ? <p className="text-sm text-negative">{statusError}</p> : null}
        {!kiteOk ? (
          <p className="text-sm text-muted-foreground">Connect Kite on the desk before confirming a live order.</p>
        ) : null}

        <fieldset className="space-y-2">
          <legend className="text-sm font-medium">Product</legend>
          <div className="flex gap-2">
            {(["CNC", "MTF"] as const).map((item) => (
              <Button
                key={item}
                type="button"
                size="sm"
                variant={product === item ? "secondary" : "outline"}
                aria-pressed={product === item}
                onClick={() => {
                  setProduct(item);
                  setPlan(null);
                  setOutcome(null);
                }}
              >
                {item}
              </Button>
            ))}
          </div>
          <p className="text-xs text-muted-foreground">Default CNC. MTF is an explicit click. MIS is not offered.</p>
        </fieldset>

        <div className="space-y-1.5">
          <Label htmlFor={`buy-amount-${symbol}`}>Amount to invest (₹)</Label>
          <Input
            id={`buy-amount-${symbol}`}
            type="number"
            inputMode="decimal"
            min={1}
            step={1}
            value={amount}
            onChange={(event) => {
              setAmount(event.target.value);
              setPlan(null);
              setOutcome(null);
            }}
          />
        </div>

        <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
          <dt className="text-muted-foreground">Last price</dt>
          <dd className="tabular-nums">{formatInr(lastPrice)}</dd>
          <dt className="text-muted-foreground">Estimated qty</dt>
          <dd className="tabular-nums">{qty > 0 ? qty : "—"}</dd>
          <dt className="text-muted-foreground">Notional</dt>
          <dd className="tabular-nums">{qty > 0 ? formatInr(notional) : "—"}</dd>
          {plan?.required_margin_inr ? (
            <>
              <dt className="text-muted-foreground">Required margin (Kite)</dt>
              <dd className="tabular-nums">{formatInr(Number(plan.required_margin_inr))}</dd>
            </>
          ) : null}
        </dl>

        {product === "MTF" ? (
          <p className="text-xs text-muted-foreground">
            MTF is leveraged: you borrow from Zerodha against the shares. The same-session GTT stop is
            still required. Kite GTT does not accept product MTF — the stop is a CNC cash trigger that
            sells the shares.
          </p>
        ) : (
          <p className="text-xs text-muted-foreground">A same-session GTT stop is required (vol-scaled 8–12%).</p>
        )}

        {plan?.gtt_note && product === "MTF" ? (
          <p className="text-xs text-muted-foreground">{plan.gtt_note}</p>
        ) : null}

        {error ? <p className="text-sm text-negative">{error}</p> : null}

        {outcome ? (
          <p className="rounded-md border border-border px-3 py-2 text-sm" data-testid="buy-outcome">
            {outcome.status}
            {outcome.reason ? ` — ${outcome.reason}` : ""}
            {outcome.simulated ? " (simulated)" : ""}
          </p>
        ) : null}

        <div className="flex flex-col gap-2">
          <Button type="button" variant="outline" disabled={busy !== null || !(Number(amount) > 0) || qty <= 0} onClick={() => void onReview()}>
            {busy === "analyze" ? "Sizing…" : "Review plan"}
          </Button>
          <Button
            type="button"
            variant={live ? "destructive" : "primary"}
            disabled={
              busy !== null ||
              !plan?.confirmable ||
              !plan.plan_id ||
              plan.line_id == null ||
              Boolean(outcome) ||
              (live && !kiteOk)
            }
            onClick={() => void onConfirm()}
          >
            {busy === "execute" ? "Sending…" : confirmText || "Confirm"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

export function OptionsBuyUnavailable() {
  return (
    <Button
      type="button"
      size="sm"
      variant="outline"
      disabled
      title="Options Buy is not wired — the desk F&O execute path is F3 credit spreads, not these structures."
    >
      Buy
    </Button>
  );
}
