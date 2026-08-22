import type { Metadata } from "next";
import Link from "next/link";

import { BasketUnavailable, fetchLatestPlan } from "@/lib/basket/fetch";

/**
 * `/baskets/plan` — M22 §2. The desk's most recent rebalance plan, in full.
 *
 * Every column the desk's own Jinja table shows, including what filled and at what price. It is a
 * record of a decision, not a control surface: **there is no execute button here, and no route
 * behind one.** The desk console is the only place an order can be placed, which is what keeps
 * this page on the safe side of the SEBI gate.
 */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Rebalance plan",
  description:
    "The desk's most recent rebalance plan: every order, its planned and filled quantity, and " +
    "the price each was actually done at.",
};

function money(value: number | null): string {
  return value === null ? "–" : value.toFixed(2);
}

export default async function PlanPage() {
  let plan;
  try {
    plan = await fetchLatestPlan();
  } catch (error) {
    if (!(error instanceof BasketUnavailable)) throw error;
    return (
      <div className="flex flex-col gap-4">
        <h1 className="text-2xl font-semibold tracking-tight">Rebalance plan</h1>
        <p className="text-sm text-muted-foreground">
          The desk has recorded no plans yet.
        </p>
      </div>
    );
  }

  const buys = plan.orders.filter((order) => order.side.toUpperCase() === "BUY");
  const sells = plan.orders.filter((order) => order.side.toUpperCase() !== "BUY");

  return (
    <div className="flex flex-col gap-5">
      <header className="flex flex-col gap-1">
        <h1 className="text-2xl font-semibold tracking-tight">Rebalance plan</h1>
        <p className="text-sm text-muted-foreground">
          <code>{plan.plan_id}</code> · built {new Date(plan.created_at).toLocaleString("en-IN")}
          {plan.note ? ` · ${plan.note}` : ""}
        </p>
      </header>

      <section className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat label="Orders" value={String(plan.orders.length)} />
        <Stat label="Buys" value={String(buys.length)} />
        <Stat label="Sells" value={String(sells.length)} />
        <Stat label="Constituents" value={String(plan.constituents.length)} />
      </section>

      <div className="overflow-x-auto">
        <table className="w-full text-sm tabular-nums">
          <caption className="sr-only">Every order in plan {plan.plan_id}</caption>
          <thead>
            <tr className="border-b text-left text-xs uppercase tracking-wide text-muted-foreground">
              <th scope="col" className="py-2 pr-3">Symbol</th>
              <th scope="col" className="py-2 pr-3">Side</th>
              <th scope="col" className="py-2 pr-3 text-right">Planned qty</th>
              <th scope="col" className="py-2 pr-3 text-right">Ref price</th>
              <th scope="col" className="py-2 pr-3 text-right">Filled</th>
              <th scope="col" className="py-2 pr-3 text-right">Avg fill</th>
              <th scope="col" className="py-2 pr-3">Status</th>
              <th scope="col" className="py-2 pr-3">Broker order</th>
            </tr>
          </thead>
          <tbody>
            {plan.orders.map((order) => (
              <tr key={`${order.symbol}-${order.side}`} className="border-b last:border-0">
                <td className="py-2 pr-3 font-medium">{order.symbol}</td>
                <td className="py-2 pr-3">{order.side}</td>
                <td className="py-2 pr-3 text-right">{order.planned_qty}</td>
                <td className="py-2 pr-3 text-right">{money(order.planned_ref_price)}</td>
                <td className="py-2 pr-3 text-right">
                  {order.filled_qty === null ? "–" : order.filled_qty}
                </td>
                <td className="py-2 pr-3 text-right">{money(order.avg_fill_price)}</td>
                <td className="py-2 pr-3">{order.status ?? "–"}</td>
                <td className="py-2 pr-3 text-xs text-muted-foreground">
                  {order.order_id ?? "–"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {Object.keys(plan.weights).length > 0 && (
        <details className="rounded-xl border border-border/70 bg-card p-4 text-sm">
          <summary className="cursor-pointer font-medium">Target weights</summary>
          <ul className="mt-2 grid grid-cols-2 gap-x-6 gap-y-1 sm:grid-cols-3">
            {Object.entries(plan.weights)
              .sort(([, a], [, b]) => b - a)
              .map(([symbol, weight]) => (
                <li key={symbol} className="flex justify-between tabular-nums">
                  <span>{symbol}</span>
                  <span className="text-muted-foreground">{weight.toFixed(2)}%</span>
                </li>
              ))}
          </ul>
        </details>
      )}

      <nav aria-label="Related" className="flex flex-wrap gap-4 text-sm">
        <Link className="underline underline-offset-4" href="/baskets">
          Today&apos;s basket
        </Link>
        <Link className="underline underline-offset-4" href="/portfolios">
          Rebalance tracker
        </Link>
        <Link className="underline underline-offset-4" href="/backtests">
          Backtests
        </Link>
      </nav>

      <p className="text-xs text-muted-foreground">
        This page is read-only. Orders are placed from the desk console and nowhere else.
      </p>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-xl border border-border/70 bg-card p-4">
      <div className="text-xs uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className="mt-1 text-lg font-semibold tabular-nums">{value}</div>
    </div>
  );
}
