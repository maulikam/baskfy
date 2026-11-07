"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  analyzeDeskBuy,
  confirmLabel,
  DeskBuyError,
  executeDeskBuy,
  fetchDeskTicket,
  fetchOrderStatus,
  materialChange,
  reconcileFills,
  STATUS_LABEL,
  TERMINAL_STATUSES,
  type AmountBasis,
  type BuySource,
  type CashProduct,
  type DeskAnalyze,
  type DeskExecute,
  type DeskOrderStatus,
  type DeskTicket,
  type OrderType,
} from "@/lib/opportunities/desk-buy";
import { formatInr } from "@/lib/opportunities/view";

export interface BuyTicketProps {
  symbol: string;
  name?: string | null;
  instrumentId: number;
  exchange?: string;
  price: number | null;
  source: BuySource;
  setup?: string | null;
  entry?: number | null;
  stop?: number | null;
  target?: number | null;
  lotSize?: number;
  disabledReason?: string | null;
}

function inr(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const n = Number(value);
  return Number.isFinite(n) ? formatInr(n) : "—";
}

function Segmented<T extends string>({
  value,
  options,
  onChange,
  disabled,
}: {
  value: T;
  options: readonly { value: T; label: string; disabled?: boolean; title?: string }[];
  onChange: (v: T) => void;
  disabled?: boolean;
}) {
  return (
    <div className="flex flex-wrap gap-2">
      {options.map((o) => (
        <Button
          key={o.value}
          type="button"
          size="sm"
          variant={value === o.value ? "secondary" : "outline"}
          aria-pressed={value === o.value}
          disabled={disabled || o.disabled}
          title={o.title}
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </Button>
      ))}
    </div>
  );
}

function Row({ k, v, hint }: { k: string; v: string; hint?: string | undefined }) {
  return (
    <>
      <dt className="text-muted-foreground" title={hint}>
        {k}
      </dt>
      <dd className="text-right tabular-nums">{v}</dd>
    </>
  );
}

export function BuyTicketButton(props: BuyTicketProps) {
  const disabled =
    Boolean(props.disabledReason) || !(props.instrumentId > 0) || !(props.price && props.price > 0);
  const reason =
    props.disabledReason ??
    (!(props.instrumentId > 0) ? "No instrument mapping for this row." : !(props.price && props.price > 0) ? "No price for this row." : undefined);
  const [open, setOpen] = useState(false);
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={disabled}
          title={disabled ? reason : `Open an order ticket for ${props.symbol}. Nothing is sent until you press Place Buy Order.`}
          data-testid="buy-button"
          onClick={(e) => e.stopPropagation()}
        >
          Buy
        </Button>
      </DialogTrigger>
      {disabled && reason ? <span className="sr-only">{reason}</span> : null}
      <DialogContent className="max-h-[90vh] max-w-lg space-y-4 overflow-y-auto" onClick={(e) => e.stopPropagation()}>
        {open ? <Ticket {...props} /> : null}
      </DialogContent>
    </Dialog>
  );
}

function Ticket({
  symbol,
  name,
  instrumentId,
  exchange = "NSE",
  price,
  source,
  setup,
  entry,
  stop,
  target,
  lotSize = 1,
}: BuyTicketProps) {
  const router = useRouter();
  const [ticket, setTicket] = useState<DeskTicket | null>(null);
  const [ticketError, setTicketError] = useState<string | null>(null);
  const [product, setProduct] = useState<CashProduct>("CNC");
  const [orderType, setOrderType] = useState<OrderType>("MARKET");
  const [sizeBy, setSizeBy] = useState<"quantity" | "amount">("quantity");
  const [basis, setBasis] = useState<AmountBasis>("order_value");
  const [quantity, setQuantity] = useState("");
  const [amount, setAmount] = useState("");
  const [limit, setLimit] = useState(price ? price.toFixed(2) : "");
  const [plan, setPlan] = useState<DeskAnalyze | null>(null);
  const [changes, setChanges] = useState<string[]>([]);
  const [busy, setBusy] = useState<"ticket" | "review" | "place" | null>("ticket");
  const [error, setError] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<DeskExecute | null>(null);
  const [order, setOrder] = useState<DeskOrderStatus | null>(null);
  const placing = useRef(false);
  const reconciled = useRef(false);

  useEffect(() => {
    let cancelled = false;
    fetchDeskTicket(symbol, exchange)
      .then((t) => {
        if (cancelled) return;
        setTicket(t);
        const q = t.quote;
        if (q) setLimit((l) => l || Number(q.last_price).toFixed(2));
      })
      .catch((e: unknown) => {
        if (!cancelled) setTicketError(e instanceof Error ? e.message : "Could not reach the desk.");
      })
      .finally(() => {
        if (!cancelled) setBusy(null);
      });
    return () => {
      cancelled = true;
    };
  }, [symbol, exchange]);

  const lineId = plan?.line_id ?? null;
  const tracking = Boolean(outcome) || order !== null;
  useEffect(() => {
    if (!tracking || lineId == null) return;
    if (order && TERMINAL_STATUSES.includes(order.status) && !order.needs_reconcile) return;
    const timer = window.setTimeout(() => {
      fetchOrderStatus(lineId)
        .then(async (s) => {
          setOrder(s);
          if (s.needs_reconcile && !reconciled.current) {
            reconciled.current = true;
            await reconcileFills().catch(() => undefined);
          }
          if (TERMINAL_STATUSES.includes(s.status)) router.refresh();
        })
        .catch((e: unknown) => setError(e instanceof Error ? e.message : "Status read failed."));
    }, order ? 3000 : 500);
    return () => window.clearTimeout(timer);
  }, [tracking, lineId, order, router]);

  const quotePrice = ticket?.quote ? Number(ticket.quote.last_price) : (price ?? 0);
  const mtfOk = ticket?.mtf.eligible === true;
  const live = plan ? !plan.simulated : ticket ? !ticket.simulated : false;
  const locked = busy !== null || tracking;

  function reset() {
    setPlan(null);
    setChanges([]);
    setError(null);
  }

  const qtyNum = Math.floor(Number(quantity));
  const amtNum = Number(amount);
  const limitNum = Number(limit);
  const inputsOk =
    (sizeBy === "quantity" ? qtyNum > 0 : amtNum > 0) && (orderType === "MARKET" || limitNum > 0);

  async function review(prev?: DeskAnalyze) {
    setError(null);
    setBusy("review");
    try {
      const next = await analyzeDeskBuy({
        symbol,
        instrument_id: instrumentId,
        setup: setup ?? "",
        source,
        product,
        order_type: orderType,
        limit_price: limit,
        quantity: sizeBy === "quantity" ? qtyNum : 0,
        amount_inr: amount,
        amount_basis: product === "MTF" ? basis : "order_value",
        last_price: String(quotePrice || price || 0),
        trigger: entry && entry > 0 ? String(entry) : "",
        stop: stop && stop > 0 ? String(stop) : "",
        lot_size: lotSize,
      });
      setPlan(next);
      setChanges(prev ? materialChange(prev, next) : []);
      if (next.skip) setError(next.skip);
    } catch (e: unknown) {
      setPlan(null);
      setError(e instanceof Error ? e.message : "Review failed.");
    } finally {
      setBusy(null);
    }
  }

  async function place() {
    if (placing.current || !plan?.confirmable || !plan.plan_id || plan.line_id == null) return;
    placing.current = true;
    setError(null);
    setBusy("place");
    try {
      setOutcome(await executeDeskBuy(plan.plan_id, plan.line_id));
    } catch (e: unknown) {
      if (e instanceof DeskBuyError && e.reviewAgain) {
        placing.current = false;
        setBusy(null);
        setError(`${e.message}. The review below is refreshed — check it and place again.`);
        await review(plan);
        return;
      }
      setError(
        e instanceof DeskBuyError && e.status < 500
          ? e.message
          : "The desk did not answer cleanly. Not retrying — reading the order's state from the broker.",
      );
      try {
        const s = await fetchOrderStatus(plan.line_id);
        setOrder(s);
        if (s.status === "NOT_SENT") placing.current = false;
      } catch {
        setError("Could not confirm whether the order was sent. Check Orders on the desk before trying again.");
      }
    } finally {
      setBusy(null);
    }
  }

  const orderId = order?.order_id ?? (outcome?.order?.order_id != null ? String(outcome.order.order_id) : null);
  const status = order?.status ?? null;

  return (
    <>
      <DialogTitle>
        Buy {symbol}
        <span className="ml-2 text-sm font-normal text-muted-foreground">{exchange}</span>
      </DialogTitle>
      <DialogDescription>
        {name ? `${name} · ` : ""}From {source}. Review sizes it with Zerodha&rsquo;s margin; nothing is sent until you press Place Buy Order.
      </DialogDescription>

      {ticketError ? <p className="text-sm text-negative">{ticketError}</p> : null}
      {ticket ? (
        <div className="rounded-md border border-border px-3 py-2 text-xs" data-testid="buy-account">
          {ticket.kite_authed && ticket.account ? (
            <>
              {ticket.account.broker} · {ticket.account.user_id}
              {ticket.account.user_name ? ` (${ticket.account.user_name})` : ""} · available {inr(ticket.available_funds)}
            </>
          ) : (
            <span className="text-negative">Kite is not connected — connect on the desk to quote margin and place orders.</span>
          )}
          <div className="mt-1 text-muted-foreground">
            {ticket.quote ? (
              <>
                Last {inr(ticket.quote.last_price)}
                {ticket.quote.at ? ` at ${ticket.quote.at}` : ""}
                {ticket.quote.stale ? <span className="ml-1 text-warning">stale</span> : null}
              </>
            ) : (
              <>Page price {inr(price)} — no live broker quote</>
            )}
            {ticket.market_open ? "" : " · market closed"}
          </div>
          {!ticket.new_entries_allowed ? (
            <p className="mt-1 text-negative">Market gate {ticket.gate}: new entries are closed, so the desk will refuse a buy.</p>
          ) : null}
        </div>
      ) : busy === "ticket" ? (
        <p className="text-sm text-muted-foreground">Reading account, quote and MTF eligibility…</p>
      ) : null}

      {live ? (
        <p className="rounded-md border border-negative/40 bg-negative/10 px-3 py-2 text-sm" data-testid="buy-live-banner">
          Place Buy Order sends a <strong>real Zerodha order</strong>.
        </p>
      ) : (
        <p className="rounded-md border border-border bg-muted px-3 py-2 text-sm" data-testid="buy-sim-banner">
          This stack simulates{ticket?.dry_run ? " (DRY_RUN)" : ticket && !ticket.swing_execution_enabled ? " (execution off)" : ""}: no order reaches the broker.
        </p>
      )}

      <fieldset className="space-y-1.5" disabled={locked}>
        <legend className="text-sm font-medium">Product</legend>
        <Segmented
          value={product}
          onChange={(v) => {
            setProduct(v);
            reset();
          }}
          options={[
            { value: "CNC", label: "Cash / Delivery (CNC)" },
            {
              value: "MTF",
              label: "MTF",
              disabled: !mtfOk,
              title: mtfOk ? "Margin trading facility — Zerodha funds part of the order" : (ticket?.mtf.reason ?? "MTF eligibility unknown"),
            },
          ]}
        />
        {!mtfOk && ticket ? <p className="text-xs text-muted-foreground">MTF unavailable: {ticket.mtf.reason}</p> : null}
      </fieldset>

      <fieldset className="space-y-1.5" disabled={locked}>
        <legend className="text-sm font-medium">Order type</legend>
        <Segmented
          value={orderType}
          onChange={(v) => {
            setOrderType(v);
            reset();
          }}
          options={[
            { value: "MARKET", label: "Market" },
            { value: "LIMIT", label: "Limit" },
          ]}
        />
        {orderType === "LIMIT" ? (
          <div className="space-y-1">
            <Label htmlFor={`buy-limit-${symbol}`}>Limit price (₹)</Label>
            <Input
              id={`buy-limit-${symbol}`}
              type="number"
              inputMode="decimal"
              min={0.05}
              step={0.05}
              value={limit}
              onChange={(e) => {
                setLimit(e.target.value);
                reset();
              }}
            />
          </div>
        ) : null}
      </fieldset>

      <fieldset className="space-y-1.5" disabled={locked}>
        <legend className="text-sm font-medium">Size</legend>
        <Segmented
          value={sizeBy}
          onChange={(v) => {
            setSizeBy(v);
            reset();
          }}
          options={[
            { value: "quantity", label: "Quantity" },
            { value: "amount", label: "Amount (₹)" },
          ]}
        />
        {sizeBy === "quantity" ? (
          <Input
            aria-label="Quantity"
            type="number"
            inputMode="numeric"
            min={lotSize}
            step={lotSize}
            value={quantity}
            onChange={(e) => {
              setQuantity(e.target.value);
              reset();
            }}
          />
        ) : (
          <>
            {product === "MTF" ? (
              <Segmented
                value={basis}
                onChange={(v) => {
                  setBasis(v);
                  reset();
                }}
                options={[
                  { value: "order_value", label: "Total order value" },
                  { value: "own", label: "My own capital" },
                ]}
              />
            ) : null}
            <Input
              aria-label={product === "MTF" && basis === "own" ? "Own capital (₹)" : "Total order value (₹)"}
              type="number"
              inputMode="decimal"
              min={1}
              value={amount}
              onChange={(e) => {
                setAmount(e.target.value);
                reset();
              }}
            />
            <p className="text-xs text-muted-foreground">
              {product === "MTF" && basis === "own"
                ? "Quantity is the most whose Zerodha MTF margin plus charges fits this amount."
                : "Quantity is this amount ÷ order price, rounded down."}
            </p>
          </>
        )}
      </fieldset>

      {entry || stop || target ? (
        <dl className="grid grid-cols-2 gap-x-3 gap-y-1 rounded-md border border-border/60 px-3 py-2 text-xs">
          <dt className="col-span-2 font-medium">Setup reference{setup ? ` · ${setup}` : ""}</dt>
          {entry ? <Row k="Entry / trigger" v={inr(entry)} /> : null}
          {stop ? <Row k="Stop" v={inr(stop)} /> : null}
          {target ? <Row k="Target" v={inr(target)} hint="Reference only — no target order is placed" /> : null}
        </dl>
      ) : null}

      {plan ? (
        <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-xs" data-testid="buy-review">
          <dt className="col-span-2 font-medium">Review</dt>
          <Row k="Quantity" v={String(plan.quantity)} />
          <Row k={plan.order_type === "LIMIT" ? "Limit price" : "Market price (quote)"} v={inr(plan.order_price)} />
          <Row k="Quote" v={`${plan.quote_live ? (plan.quote_at ?? "live") : "no broker quote"}${plan.stale_quote ? " · stale" : ""}`} />
          <Row k="Order value" v={inr(plan.order_value)} hint="quantity × order price" />
          <Row k="Own funds required (Zerodha margin)" v={inr(plan.own_contribution)} />
          {plan.product === "MTF" ? (
            <>
              <Row k="Broker-funded" v={inr(plan.broker_funding)} hint="order value − own funds" />
              <Row k="Leverage (Zerodha)" v={plan.leverage ? `${plan.leverage}×` : "—"} />
              <Row
                k={`Est. interest / day (${plan.mtf_interest_pct_per_day}%)`}
                v={inr(plan.mtf_interest_per_day)}
                hint={ticket?.mtf.interest_basis}
              />
            </>
          ) : null}
          <Row k="Charges (est., Zerodha)" v={inr(plan.charges)} />
          <Row k="Available funds" v={inr(plan.available_funds)} />
          <Row
            k={`GTT stop after fill${plan.stop_basis === "band" ? " (band default)" : ""}`}
            v={inr(plan.stop)}
            hint={plan.gtt_note}
          />
        </dl>
      ) : null}
      {plan?.product === "MTF" ? (
        <p className="text-xs text-muted-foreground">
          Interest is an estimate: {plan.mtf_interest_pct_per_day}% a day on the broker-funded amount. Zerodha&rsquo;s contract note is the actual charge.
        </p>
      ) : null}
      {plan ? <p className="text-xs text-muted-foreground">{plan.gtt_note}</p> : null}
      {plan?.margin_error ? <p className="text-xs text-warning">Margin quote: {plan.margin_error}</p> : null}
      {changes.length > 0 ? (
        <p className="text-sm text-warning" data-testid="buy-changed">
          Changed since your review: {changes.join("; ")}.
        </p>
      ) : null}

      {error ? (
        <p className="text-sm text-negative" data-testid="buy-error">
          {error}
        </p>
      ) : null}

      {outcome || order ? (
        <div className="rounded-md border border-border px-3 py-2 text-sm" data-testid="buy-outcome">
          <div className="font-medium">{status ? STATUS_LABEL[status] : outcome?.status}</div>
          {orderId ? <div className="text-xs">Broker order {orderId}</div> : null}
          {order?.broker ? (
            <div className="text-xs text-muted-foreground">
              {order.broker.status}
              {order.broker.filled_quantity != null ? ` · filled ${order.broker.filled_quantity}/${order.quantity}` : ""}
              {order.broker.average_price ? ` @ ${inr(order.broker.average_price)}` : ""}
              {order.broker.status_message ? ` · ${order.broker.status_message}` : ""}
            </div>
          ) : null}
          {outcome?.reason ? <div className="text-xs text-muted-foreground">{outcome.reason}</div> : null}
          {status && !TERMINAL_STATUSES.includes(status) ? (
            <div className="text-xs text-muted-foreground">Submitted is not bought — watching the broker for fills.</div>
          ) : null}
        </div>
      ) : null}

      <div className="flex flex-col gap-2">
        <Button type="button" variant="outline" disabled={locked || !inputsOk || !ticket} onClick={() => void review()}>
          {busy === "review" ? "Reviewing…" : plan ? "Review again" : "Review order"}
        </Button>
        <Button
          type="button"
          variant={live ? "destructive" : "primary"}
          disabled={locked || !plan?.confirmable}
          onClick={() => void place()}
          data-testid="buy-place"
        >
          {busy === "place"
            ? "Placing…"
            : plan?.confirmable
              ? confirmLabel(live, plan.product, plan.quantity, symbol)
              : "Place Buy Order"}
        </Button>
      </div>
    </>
  );
}
