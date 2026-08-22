import type { Metadata } from "next";
import Link from "next/link";

import { BasketUnavailable, fetchBasket } from "@/lib/basket/fetch";

/**
 * `/baskets` — M22. What the momentum strategy wants to hold today.
 *
 * Built from the live MomentumScan, the same scoring the desk uses, and the same basket engine.
 * **Read-only.** There is no execute control on this page and there is no route behind one:
 * execution lives in the desk console, which is the only place an order can be placed.
 */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Baskets",
  description:
    "The momentum basket as the strategy would construct it today: names, weights, scores and " +
    "the stop each position would carry.",
};

function pct(value: number): string {
  return `${value.toFixed(2)}%`;
}

function rupees(value: number): string {
  return `₹${value.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
}

export default async function BasketsPage() {
  let basket;
  try {
    basket = await fetchBasket();
  } catch (error) {
    if (!(error instanceof BasketUnavailable)) throw error;
    return (
      <div className="flex flex-col gap-4">
        <h1 className="text-2xl font-semibold tracking-tight">Baskets</h1>
        <p className="text-sm text-muted-foreground">
          No basket is available. It needs bars in the pipeline and one uploaded scan to take
          market cap, beta, circuit counts and F&amp;O membership from — nothing computes those
          from prices.
        </p>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-5">
      <header className="flex flex-col gap-1">
        <h1 className="text-2xl font-semibold tracking-tight">Baskets</h1>
        <p className="text-sm text-muted-foreground">
          As of {basket.as_of} · run <code>{basket.screen_run_id}</code> · data version{" "}
          {basket.data_version}
        </p>
      </header>

      <section className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat label="Names" value={String(basket.rows.length)} />
        <Stat label="Notional" value={rupees(basket.capital)} />
        <Stat label="Cash target" value={pct(basket.cash_target_pct)} />
        <Stat label="Breadth &gt; 20DMA" value={pct(basket.breadth_above_20dma)} />
      </section>

      {basket.suspect_symbols.length > 0 && (
        <p
          role="status"
          className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-sm"
        >
          <strong>{basket.suspect_symbols.length}</strong> of the scanned symbols carry an
          unadjusted corporate action. Their returns and distance-from-high are computed against
          prices that were never adjusted for a split or bonus, so some are scored wrongly and
          some are excluded from this basket that should not be.
        </p>
      )}

      <div className="overflow-x-auto">
        <table className="w-full text-sm tabular-nums">
          <caption className="sr-only">
            The momentum basket for {basket.as_of}, by rank
          </caption>
          <thead>
            <tr className="border-b text-left text-xs uppercase tracking-wide text-muted-foreground">
              <th scope="col" className="py-2 pr-3">#</th>
              <th scope="col" className="py-2 pr-3">Symbol</th>
              <th scope="col" className="py-2 pr-3 text-right">Score</th>
              <th scope="col" className="py-2 pr-3 text-right">Weight</th>
              <th scope="col" className="py-2 pr-3 text-right">Price</th>
              <th scope="col" className="py-2 pr-3 text-right">Stop</th>
              <th scope="col" className="py-2 pr-3 text-right">Value</th>
              <th scope="col" className="py-2 pr-3 text-right" title="Trend / Momentum / Sharpe / Consistency / Liquidity / Penalty">
                A · B · C · D · E · F
              </th>
            </tr>
          </thead>
          <tbody>
            {basket.rows.map((row) => (
              <tr key={row.symbol} className="border-b last:border-0">
                <td className="py-2 pr-3 text-muted-foreground">{row.rank}</td>
                <td className="py-2 pr-3 font-medium">{row.symbol}</td>
                <td className="py-2 pr-3 text-right">{row.score.toFixed(1)}</td>
                <td className="py-2 pr-3 text-right">{pct(row.weight)}</td>
                <td className="py-2 pr-3 text-right">{row.ref_price.toFixed(2)}</td>
                <td className="py-2 pr-3 text-right">{row.stop.toFixed(1)}</td>
                <td className="py-2 pr-3 text-right">{rupees(row.value)}</td>
                <td className="py-2 pr-3 text-right text-xs text-muted-foreground">
                  {[row.a_trend, row.b_momentum, row.c_sharpe, row.d_consistency,
                    row.e_liquidity, row.f_penalty]
                    .map((part) => (part === null ? "–" : part.toFixed(0)))
                    .join(" · ")}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <nav aria-label="Related" className="flex flex-wrap gap-4 text-sm">
        <Link className="underline underline-offset-4" href="/baskets/plan">
          The latest rebalance plan
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
    <div className="rounded-lg border p-3">
      <div className="text-xs uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className="mt-1 text-lg font-semibold tabular-nums">{value}</div>
    </div>
  );
}
