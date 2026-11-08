"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { SeriesFlag, seriesNote } from "@/components/regime-signal/series-flag";
import {
  analyzeDeskBuy,
  DeskBuyError,
  executeDeskBuy,
  fetchMtfLeverage,
  fetchOrderStatus,
  STATUS_LABEL,
  TERMINAL_STATUSES,
  type AmountBasis,
  type CashProduct,
  type DeskAnalyze,
  type DeskOrderStatus,
  type OrderType,
} from "@/lib/opportunities/desk-buy";
import { SPLIT_HINT, SPLIT_LABEL, splitAmount, type SplitMode } from "@/lib/opportunities/multi-buy";
import type { SwingOpportunity } from "@/lib/opportunities/types";
import { formatInr } from "@/lib/opportunities/view";
import { useLiveMarks } from "@/lib/screens/live-marks";

type Line = {
  plan?: DeskAnalyze;
  error?: string;
  sent?: string;
  order?: DeskOrderStatus;
};

function Choice<T extends string>({
  value,
  options,
  onChange,
  disabled,
}: {
  value: T;
  options: readonly { value: T; label: string; title?: string }[];
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
          disabled={disabled}
          title={o.title}
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </Button>
      ))}
    </div>
  );
}

function inr(v: string | number | null | undefined): string {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  return Number.isFinite(n) ? formatInr(n) : "—";
}

const sleep = (ms: number) => new Promise((r) => window.setTimeout(r, ms));

export function MultiBuyButton({ rows }: { rows: readonly SwingOpportunity[] }) {
  const [open, setOpen] = useState(false);
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button type="button" size="sm" variant="primary" disabled={rows.length === 0} data-testid="multi-buy-button">
          Buy selected ({rows.length})
        </Button>
      </DialogTrigger>
      <DialogContent className="max-h-[90vh] max-w-3xl space-y-4 overflow-y-auto">
        {open ? <MultiBuy rows={rows} /> : null}
      </DialogContent>
    </Dialog>
  );
}

function MultiBuy({ rows: picked }: { rows: readonly SwingOpportunity[] }) {
  // One line per stock: a stock that matched two setups keeps its higher-scoring one.
  const rows = useMemo(() => {
    const best = new Map<string, SwingOpportunity>();
    for (const r of picked) {
      const seen = best.get(r.symbol);
      if (!seen || r.score.total > seen.score.total) best.set(r.symbol, r);
    }
    return [...best.values()];
  }, [picked]);
  const symbols = useMemo(() => rows.map((r) => r.symbol), [rows]);
  const live = useLiveMarks(symbols);
  const [total, setTotal] = useState("");
  const [mode, setMode] = useState<SplitMode>("equal");
  const [product, setProduct] = useState<CashProduct>("CNC");
  const [basis, setBasis] = useState<AmountBasis>("order_value");
  const [orderType, setOrderType] = useState<OrderType>("MARKET");
  const [halveFlagged, setHalveFlagged] = useState(true);
  const flagged = rows.filter((r) => seriesNote(r.series) !== null).length;
  const [lines, setLines] = useState<Record<string, Line>>({});
  const [busy, setBusy] = useState<"review" | "place" | null>(null);
  const placing = useRef(false);
  const [leverage, setLeverage] = useState<Record<string, number | null>>({});

  const [leverageError, setLeverageError] = useState<string | null>(null);

  // MTF: every stock's Zerodha leverage in one desk call (stored each morning), so "my capital"
  // turns into an order value before Review.
  const symbolKey = [...new Set(symbols)].sort().join(",");
  useEffect(() => {
    if (product !== "MTF" || symbolKey === "") return;
    let live = true;
    fetchMtfLeverage(symbolKey.split(","))
      .then((res) => {
        if (!live) return;
        const next: Record<string, number | null> = {};
        for (const sym of symbolKey.split(",")) {
          const row = res.data[sym];
          const lev = row?.eligible && row.leverage ? Number(row.leverage) : null;
          next[sym] = lev && lev > 0 ? lev : null;
        }
        setLeverage(next);
        setLeverageError(null);
      })
      .catch((e: unknown) => {
        if (live) setLeverageError(e instanceof Error ? e.message : "Could not read MTF leverage.");
      });
    return () => {
      live = false;
    };
  }, [product, symbolKey]);

  const priced = rows.map((r) => {
    const price = live.marks[r.symbol] ?? live.quotes[r.symbol]?.lastPrice ?? r.close ?? r.entry.price;
    const stop = r.invalidation > 0 && r.invalidation < price ? r.invalidation : null;
    return { row: r, price, stop };
  });
  const split = splitAmount(
    Number(total),
    priced.map((p) => ({
      symbol: p.row.symbol,
      price: p.price,
      stop: p.stop,
      target: p.row.targets[0]?.price ?? null,
      riskReward: p.row.risk_reward,
      score: p.row.score.total,
      scale: halveFlagged && seriesNote(p.row.series) ? 0.5 : 1,
    })),
    mode,
  );
  const ownBasis = product === "MTF" && basis === "own";
  // Per row: the order value, own funds, quantity, and loss/profit from that quantity. After Review the desk's
  // own numbers replace the estimate.
  const sized = priced.map((p, i) => {
    const amount = split[i]?.amount ?? 0;
    const plan = lines[p.row.symbol]?.plan;
    const lev = product === "MTF" ? (leverage[p.row.symbol] ?? null) : 1;
    const price = plan ? Number(plan.order_price) : p.price;
    // "My capital" buys capital × leverage of stock; "order value" is the stock bought.
    const budget = ownBasis ? (lev ? amount * lev : null) : amount;
    const quantity = plan ? plan.quantity : budget !== null && price > 0 ? Math.floor(budget / price) : null;
    const orderValue = plan ? Number(plan.order_value) : quantity !== null ? quantity * price : null;
    const own = plan?.own_contribution
      ? Number(plan.own_contribution)
      : orderValue !== null && lev
        ? orderValue / lev
        : null;
    const target = p.row.targets[0]?.price ?? null;
    const loss = quantity !== null && p.stop !== null && p.stop < price ? quantity * (price - p.stop) : null;
    const profit = quantity !== null && target !== null && target > price ? quantity * (target - price) : null;
    return { price, orderValue, own, quantity, loss, profit };
  });
  const reviewed = Object.keys(lines).length > 0;
  const confirmable = priced.filter((p) => lines[p.row.symbol]?.plan?.confirmable && !lines[p.row.symbol]?.sent);
  const simulated = Object.values(lines).some((l) => l.plan?.simulated === true);
  const liveOrders = reviewed && !simulated;
  const sentAny = Object.values(lines).some((l) => l.sent);
  const locked = busy !== null || sentAny;

  function reset() {
    if (!sentAny) setLines({});
  }

  async function review() {
    setBusy("review");
    const next: Record<string, Line> = {};
    for (const [i, p] of priced.entries()) {
      const amount = split[i]?.amount ?? 0;
      if (amount <= 0) {
        next[p.row.symbol] = { error: "No money allocated to this stock in this split." };
        continue;
      }
      const trigger = p.row.trigger.level || p.row.entry.price;
      try {
        const plan = await analyzeDeskBuy({
          symbol: p.row.symbol,
          instrument_id: p.row.instrument_id,
          setup: p.row.setup,
          source: "opportunities",
          product,
          order_type: orderType,
          limit_price: orderType === "LIMIT" ? trigger.toFixed(2) : "",
          quantity: 0,
          amount_inr: String(amount),
          amount_basis: product === "MTF" ? basis : "order_value",
          last_price: String(p.price),
          trigger: trigger > 0 ? String(trigger) : "",
          stop: p.row.invalidation > 0 && p.row.invalidation < trigger ? String(p.row.invalidation) : "",
          lot_size: 1,
        });
        next[p.row.symbol] = plan.skip ? { plan, error: plan.skip } : { plan };
      } catch (e: unknown) {
        next[p.row.symbol] = { error: e instanceof Error ? e.message : "Review failed." };
      }
      setLines({ ...next });
    }
    setBusy(null);
  }

  async function track(symbol: string, lineId: number) {
    for (let i = 0; i < 20; i += 1) {
      await sleep(i === 0 ? 500 : 3000);
      try {
        const s = await fetchOrderStatus(lineId);
        setLines((prev) => ({ ...prev, [symbol]: { ...prev[symbol], order: s } }));
        if (TERMINAL_STATUSES.includes(s.status)) return;
      } catch {
        return;
      }
    }
  }

  async function placeAll() {
    if (placing.current) return;
    placing.current = true;
    setBusy("place");
    for (const p of confirmable) {
      const plan = lines[p.row.symbol]?.plan;
      if (!plan?.plan_id || plan.line_id == null) continue;
      const symbol = p.row.symbol;
      try {
        const out = await executeDeskBuy(plan.plan_id, plan.line_id);
        setLines((prev) => {
          const line: Line = { ...prev[symbol], sent: out.status };
          if (out.reason) line.error = out.reason;
          return { ...prev, [symbol]: line };
        });
        void track(symbol, plan.line_id);
      } catch (e: unknown) {
        const msg =
          e instanceof DeskBuyError && e.status < 500
            ? e.message
            : "The desk did not answer cleanly — not retrying. Check Orders on the desk.";
        setLines((prev) => ({ ...prev, [symbol]: { ...prev[symbol], sent: "ERROR", error: msg } }));
      }
    }
    setBusy(null);
  }

  const totalNum = Number(total);
  const sum = (f: (r: (typeof sized)[number]) => number | null) => sized.reduce((acc, r) => acc + (f(r) ?? 0), 0);
  const totalOrder = sum((r) => r.orderValue);
  const totalOwn = sum((r) => r.own);
  const risk = sum((r) => r.loss);
  const reward = sum((r) => r.profit);
  const levPending = product === "MTF" && leverageError === null && symbols.some((sym) => !(sym in leverage));

  return (
    <>
      <DialogTitle>Buy {rows.length} stocks</DialogTitle>
      <DialogDescription>
        Splits one amount across the ticked stocks. Each stock becomes its own desk order with a GTT stop after the
        fill. Nothing is sent until you press Place.
      </DialogDescription>

      {liveOrders ? (
        <p className="rounded-md border border-negative/40 bg-negative/10 px-3 py-2 text-sm" data-testid="multi-buy-live">
          Place sends <strong>real Zerodha orders</strong>, one per stock.
        </p>
      ) : reviewed ? (
        <p className="rounded-md border border-border bg-muted px-3 py-2 text-sm">
          This stack simulates: no order reaches the broker.
        </p>
      ) : null}

      <div className="grid gap-4 sm:grid-cols-2">
        <div className="space-y-1.5">
          <Label htmlFor="multi-buy-total">
            {product === "MTF" && basis === "own" ? "Own capital to use (₹)" : "Total order value (₹)"}
          </Label>
          <Input
            id="multi-buy-total"
            type="number"
            inputMode="decimal"
            min={1}
            value={total}
            disabled={locked}
            onChange={(e) => {
              setTotal(e.target.value);
              reset();
            }}
          />
        </div>
        <fieldset className="space-y-1.5" disabled={locked}>
          <legend className="text-sm font-medium">Split</legend>
          <Choice
            value={mode}
            disabled={locked}
            onChange={(v) => {
              setMode(v);
              reset();
            }}
            options={(Object.keys(SPLIT_LABEL) as SplitMode[]).map((m) => ({
              value: m,
              label: SPLIT_LABEL[m],
              title: SPLIT_HINT[m],
            }))}
          />
          <p className="text-xs text-muted-foreground">{SPLIT_HINT[mode]}</p>
        </fieldset>
        <fieldset className="space-y-1.5" disabled={locked}>
          <legend className="text-sm font-medium">Product</legend>
          <Choice
            value={product}
            disabled={locked}
            onChange={(v) => {
              setProduct(v);
              reset();
            }}
            options={[
              { value: "CNC", label: "Delivery (CNC)" },
              { value: "MTF", label: "MTF", title: "Zerodha funds part of each order; ineligible stocks are skipped" },
            ]}
          />
          {product === "MTF" ? (
            <Choice
              value={basis}
              disabled={locked}
              onChange={(v) => {
                setBasis(v);
                reset();
              }}
              options={[
                { value: "order_value", label: "Amount is order value" },
                { value: "own", label: "Amount is my capital" },
              ]}
            />
          ) : null}
        </fieldset>
        {flagged > 0 ? (
          <label className="flex items-start gap-2 text-sm sm:col-span-2">
            <input
              type="checkbox"
              className="mt-0.5 size-4 accent-primary"
              checked={halveFlagged}
              disabled={locked}
              onChange={(e) => {
                setHalveFlagged(e.target.checked);
                reset();
              }}
            />
            <span>
              Half weight for the {flagged} flagged stock{flagged === 1 ? "" : "s"} (BE / trade-for-trade / SME — thin
              liquidity). Untick a stock in the table to leave it out entirely.
            </span>
          </label>
        ) : null}
        <fieldset className="space-y-1.5" disabled={locked}>
          <legend className="text-sm font-medium">Order type</legend>
          <Choice
            value={orderType}
            disabled={locked}
            onChange={(v) => {
              setOrderType(v);
              reset();
            }}
            options={[
              { value: "MARKET", label: "Market" },
              { value: "LIMIT", label: "Limit at trigger", title: "A limit order at each setup's trigger level" },
            ]}
          />
        </fieldset>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full min-w-[46rem] text-xs" data-testid="multi-buy-lines">
          <thead className="text-left text-muted-foreground">
            <tr>
              <th className="py-1 pr-2">Stock</th>
              <th className="pr-2 text-right">Weight</th>
              <th className="pr-2 text-right">Order value</th>
              {product === "MTF" ? <th className="pr-2 text-right">Own funds</th> : null}
              <th className="pr-2 text-right">Price</th>
              <th className="pr-2 text-right">Qty</th>
              <th className="pr-2 text-right">Loss at stop</th>
              <th className="pr-2 text-right">Profit at target</th>
              <th className="pr-2">Desk</th>
            </tr>
          </thead>
          <tbody>
            {priced.map((p, i) => {
              const s = split[i];
              const z = sized[i];
              const line = lines[p.row.symbol];
              const plan = line?.plan;
              const status = line?.order?.status;
              return (
                <tr key={p.row.symbol} className="border-t border-border/40 align-top tabular-nums">
                  <td className="py-1 pr-2 font-medium">
                    {p.row.symbol}
                    <SeriesFlag series={p.row.series} />
                  </td>
                  <td className="pr-2 text-right">{s ? `${(s.weight * 100).toFixed(1)}%` : "—"}</td>
                  <td className="pr-2 text-right">{inr(z?.orderValue)}</td>
                  {product === "MTF" ? <td className="pr-2 text-right">{inr(z?.own)}</td> : null}
                  <td className="pr-2 text-right">{inr(z?.price)}</td>
                  <td className="pr-2 text-right">{z?.quantity ?? "—"}</td>
                  <td className="pr-2 text-right text-negative">{z?.loss != null ? inr(z.loss) : "—"}</td>
                  <td className="pr-2 text-right text-positive">{z?.profit != null ? inr(z.profit) : "—"}</td>
                  <td className="pr-2">
                    {status ? (
                      <span>{STATUS_LABEL[status]}</span>
                    ) : line?.sent ? (
                      <span>{line.sent}</span>
                    ) : plan?.confirmable ? (
                      <span className="text-positive">
                        Ready{plan.product === "MTF" && plan.own_contribution ? ` · own ${inr(plan.own_contribution)}` : ""}
                      </span>
                    ) : null}
                    {line?.error ? <div className="text-negative">{line.error}</div> : null}
                  </td>
                </tr>
              );
            })}
          </tbody>
          <tfoot>
            <tr className="border-t border-border font-medium tabular-nums">
              <td className="py-1 pr-2">Total</td>
              <td />
              <td className="pr-2 text-right">{inr(totalOrder)}</td>
              {product === "MTF" ? <td className="pr-2 text-right">{inr(totalOwn)}</td> : null}
              <td />
              <td />
              <td className="pr-2 text-right text-negative">{risk > 0 ? inr(risk) : "—"}</td>
              <td className="pr-2 text-right text-positive">{reward > 0 ? inr(reward) : "—"}</td>
              <td />
            </tr>
          </tfoot>
        </table>
      </div>
      <p className="text-xs text-muted-foreground">
        Quantities round down, so a little of the amount stays unspent. Loss at stop uses each setup&rsquo;s
        invalidation and profit at target its first target, both from today&rsquo;s price; the desk sets the actual
        GTT stop (vol-scaled 8–12%) and places no target order.
        {product === "MTF"
          ? " With MTF, “my capital” buys capital × each stock’s Zerodha leverage, so quantity, loss and profit scale up with it; own funds are estimated as order value ÷ leverage until Review gives Zerodha’s exact margin. Profit is before MTF interest."
          : ""}
      </p>
      {levPending ? <p className="text-xs text-muted-foreground">Reading MTF leverage from the desk…</p> : null}
      {leverageError ? <p className="text-xs text-negative">MTF leverage: {leverageError}</p> : null}

      <div className="flex flex-col gap-2 sm:flex-row">
        <Button
          type="button"
          variant="outline"
          disabled={locked || !(totalNum > 0)}
          onClick={() => void review()}
          data-testid="multi-buy-review"
        >
          {busy === "review" ? "Reviewing…" : reviewed ? "Review again" : `Review ${rows.length} orders`}
        </Button>
        <Button
          type="button"
          variant={liveOrders ? "destructive" : "primary"}
          disabled={busy !== null || confirmable.length === 0}
          onClick={() => void placeAll()}
          data-testid="multi-buy-place"
        >
          {busy === "place"
            ? "Placing…"
            : `Place ${liveOrders ? "" : "simulated "}${confirmable.length} ${product === "MTF" ? "MTF" : "delivery"} order${confirmable.length === 1 ? "" : "s"}`}
        </Button>
      </div>
    </>
  );
}
