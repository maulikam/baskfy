import type { Metadata } from "next";
import Link from "next/link";

import { PageHeader } from "@/components/shell/page-header";
import { TermHint } from "@/components/ui/term";
import { BasketUnavailable, fetchBasket } from "@/lib/basket/fetch";
import { PAGES } from "@/lib/vocabulary";
import { cn } from "@/lib/utils";

/**
 * `/baskets` — M22. What the momentum strategy wants to hold today.
 *
 * Built from the live MomentumScan, the same scoring the desk uses, and the same basket engine.
 * **Read-only.** There is no execute control on this page and there is no route behind one:
 * execution lives in the desk console, which is the only place an order can be placed.
 */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/baskets"].title,
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
      <>
        <PageHeader title={PAGES["/baskets"].title} blurb={PAGES["/baskets"].blurb} />
        <div className="grid place-items-center rounded-xl border border-dashed border-border bg-card/50 px-6 py-16 text-center">
          <p className="max-w-[52ch] text-sm leading-relaxed text-muted-foreground">
            There is no basket to show yet. Building one needs daily prices in the pipeline and one
            uploaded scan to read company size, how sharply each stock swings, and whether it
            trades in the futures market — none of which can be worked out from prices alone.
          </p>
        </div>
      </>
    );
  }

  return (
    <>
      <PageHeader
        title={PAGES["/baskets"].title}
        blurb={PAGES["/baskets"].blurb}
        meta={
          <>
            Worked out from prices up to {basket.as_of}.{" "}
            <span className="opacity-70">
              Run <code className="font-mono">{basket.screen_run_id}</code>, data version{" "}
              {basket.data_version}.
            </span>
          </>
        }
      />

      <section className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat label="How many stocks" value={String(basket.rows.length)} />
        <Stat label="Total being invested" value={rupees(basket.capital)} term="notional" />
        <Stat label="Kept as cash" value={pct(basket.cash_target_pct)} term="cash_target" />
        <Stat
          label="How many are joining in"
          value={pct(basket.breadth_above_20dma)}
          term="breadth"
        />
      </section>

      {basket.suspect_symbols.length > 0 && (
        <p
          role="status"
          className="rounded-xl border border-warning/35 bg-warning-muted p-3.5 text-sm leading-relaxed"
        >
          <strong>{basket.suspect_symbols.length} stocks here have a price history we do not fully
          trust.</strong>{" "}
          Each has had a split or a bonus issue that was never applied to its old prices, so its
          past looks like a crash that never happened. Those are scored wrongly, and a few that
          should be in this basket have been left out because of it.
        </p>
      )}

      <div className="overflow-hidden rounded-xl border border-border/70 bg-card">
        <div className="overflow-x-auto">
          <table className="w-full text-sm tabular-nums">
            <caption className="sr-only">The momentum basket for {basket.as_of}, by rank</caption>
            <thead>
              <tr className="border-b border-border bg-muted/50 text-left text-xs font-medium text-muted-foreground">
                <Th className="w-12">Rank</Th>
                <Th>Stock</Th>
                <Th align="right" term="momentum_score">
                  Momentum score
                </Th>
                <Th align="right" term="weight">
                  Share of basket
                </Th>
                <Th align="right">Price now</Th>
                <Th align="right" term="stop_price">
                  Auto-sell price
                </Th>
                <Th align="right">Amount</Th>
                <Th align="right" term="score_parts">
                  Score breakdown
                </Th>
              </tr>
            </thead>
            <tbody>
              {basket.rows.map((row) => (
                <tr
                  key={row.symbol}
                  className="border-b border-border/60 transition-colors duration-150 last:border-0 hover:bg-muted/40"
                >
                  <Td className="text-muted-foreground">{row.rank}</Td>
                  <Td className="font-medium">{row.symbol}</Td>
                  <Td align="right">{row.score.toFixed(1)}</Td>
                  <Td align="right">{pct(row.weight)}</Td>
                  <Td align="right">{row.ref_price.toFixed(2)}</Td>
                  <Td align="right">{row.stop.toFixed(1)}</Td>
                  <Td align="right">{rupees(row.value)}</Td>
                  <Td align="right" className="font-mono text-xs text-muted-foreground">
                    {[
                      row.a_trend,
                      row.b_momentum,
                      row.c_sharpe,
                      row.d_consistency,
                      row.e_liquidity,
                      row.f_penalty,
                    ]
                      .map((part) => (part === null ? "–" : part.toFixed(0)))
                      .join(" · ")}
                  </Td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <nav aria-label="Related" className="flex flex-wrap items-center gap-2 text-sm">
        <span className="text-muted-foreground">See also</span>
        {(
          [
            ["/baskets/plan", "The last plan"],
            ["/portfolios", PAGES["/portfolios"].title],
            ["/backtests", PAGES["/backtests"].title],
          ] as const
        ).map(([href, label]) => (
          <Link
            key={href}
            className="rounded-md border border-border/70 bg-card px-2.5 py-1 text-xs font-medium transition-colors duration-150 hover:border-muted-foreground/50 hover:bg-muted"
            href={href}
          >
            {label}
          </Link>
        ))}
      </nav>

      <p className="text-xs text-muted-foreground">
        This page is read-only — nothing on it can buy or sell anything. It shows what the
        strategy would hold; the orders themselves are placed somewhere else entirely.
      </p>
    </>
  );
}

function Stat({
  label,
  value,
  term,
}: {
  label: string;
  value: string;
  term?: Parameters<typeof TermHint>[0]["id"];
}) {
  return (
    <div className="rounded-xl border border-border/70 bg-card p-4">
      <div className="flex items-center gap-1.5">
        <span className="eyebrow">{label}</span>
        {term ? <TermHint id={term} /> : null}
      </div>
      <div className="mt-1.5 text-2xl font-semibold tabular-nums">{value}</div>
    </div>
  );
}

function Th({
  children,
  align,
  term,
  className,
}: {
  children: React.ReactNode;
  align?: "right";
  term?: Parameters<typeof TermHint>[0]["id"];
  className?: string;
}) {
  return (
    <th scope="col" className={cn("py-2.5 pl-4 pr-3", align === "right" && "text-right", className)}>
      <span
        className={cn("inline-flex items-center gap-1.5", align === "right" && "flex-row-reverse")}
      >
        {children}
        {term ? <TermHint id={term} /> : null}
      </span>
    </th>
  );
}

function Td({
  children,
  align,
  className,
}: {
  children: React.ReactNode;
  align?: "right";
  className?: string;
}) {
  return (
    <td className={cn("py-2.5 pl-4 pr-3", align === "right" && "text-right", className)}>
      {children}
    </td>
  );
}
