"use client";

import { useRef, useState, useTransition } from "react";

import { importTradebookAction, syncTodaysTradesAction } from "@/app/actions/trades";
import { Button } from "@/components/ui/button";
import { EMPTY_CELL, formatNumber, formatTradeDate } from "@/lib/format";
import { formatMoney } from "@/lib/portfolio/overview";
import type { BrokerTrades, TradebookImport, TradeWrite } from "@/lib/trades/fetch";

/**
 * Trade history on `/portfolio/activity` — NEEDS-MAULIK §32, built 14 Sep 2026.
 *
 * Kite's API has today's trades and nothing else, so the past comes from a Zerodha Console
 * tradebook and each new day from a capture (the worker runs one at 15:50 on session days; the
 * button runs it now). The page says both of those things, because "sync all my trades from Kite"
 * is the natural expectation and the API cannot meet it.
 */

function Report({ report }: { report: TradebookImport }) {
  return (
    <div className="space-y-2 rounded-lg border border-border bg-muted/20 p-3 text-sm" data-testid="trade-import-report">
      <p>
        {report.note ? `${report.note}. ` : ""}
        <strong>{report.inserted}</strong> new {report.inserted === 1 ? "trade" : "trades"} stored
        {report.already_present > 0 ? `, ${report.already_present} already on record` : ""}
        {report.skipped_non_equity > 0
          ? `, ${report.skipped_non_equity} derivative or currency rows left out`
          : ""}
        .
      </p>
      <p>
        <strong>{report.dated_holdings}</strong>{" "}
        {report.dated_holdings === 1 ? "holding now has its" : "holdings now have their"} purchase
        date from these trades.
      </p>
      {(report.undated_holdings ?? []).length > 0 ? (
        <div>
          <p className="text-muted-foreground">Not dated, and why:</p>
          <ul className="mt-1 list-disc pl-5 text-muted-foreground">
            {(report.undated_holdings ?? []).map((item) => (
              <li key={item.symbol}>
                {item.symbol}: holds {formatNumber(item.held)}, trades net to{" "}
                {formatNumber(item.traded_net)} — {item.reason}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {(report.unresolved_symbols ?? []).length > 0 ? (
        <p className="text-muted-foreground">
          Symbols not matched to a listed stock (kept, not used):{" "}
          {(report.unresolved_symbols ?? []).join(", ")}
        </p>
      ) : null}
    </div>
  );
}

export function TradeHistory({ trades }: { trades: BrokerTrades | null }) {
  const [result, setResult] = useState<TradeWrite | null>(null);
  const [pending, start] = useTransition();
  const form = useRef<HTMLFormElement>(null);

  const run = (action: () => Promise<TradeWrite>) =>
    start(async () => {
      setResult(await action());
    });

  const rows = trades?.rows ?? [];

  return (
    <section
      aria-labelledby="trade-history-heading"
      className="space-y-4 rounded-xl border border-border bg-card p-4"
      data-testid="trade-history"
    >
      <div className="space-y-1">
        <h2 id="trade-history-heading" className="text-base font-semibold">
          Trade history
        </h2>
        <p className="max-w-[70ch] text-sm leading-relaxed text-muted-foreground">
          Kite&apos;s API only returns today&apos;s trades, so your past trades come from Zerodha
          Console: <span className="text-foreground">Reports → Tradebook → Equity</span>, pick the
          full date range, download CSV, and upload it here. Each trading day after that is kept
          automatically at 15:50. Importing the same file again adds nothing.
        </p>
        <p className="text-xs text-muted-foreground" data-testid="trade-history-span">
          {trades && trades.total > 0
            ? `${formatNumber(trades.total)} trades on record, ${formatTradeDate(trades.first_trade_on ?? "")} to ${formatTradeDate(trades.last_trade_on ?? "")}.`
            : "No trades on record yet."}
        </p>
      </div>

      <div className="flex flex-wrap items-end gap-3">
        <form
          ref={form}
          className="flex flex-wrap items-center gap-2"
          action={(data) => run(() => importTradebookAction(data))}
        >
          <label className="text-sm">
            <span className="sr-only">Tradebook CSV</span>
            <input
              type="file"
              name="file"
              accept=".csv,text/csv"
              required
              data-testid="tradebook-file"
              className="text-sm file:mr-2 file:rounded-md file:border file:border-border file:bg-background file:px-2 file:py-1"
            />
          </label>
          <Button type="submit" size="sm" disabled={pending} data-testid="tradebook-import">
            {pending ? "Working…" : "Import tradebook"}
          </Button>
        </form>
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={pending}
          onClick={() => run(syncTodaysTradesAction)}
          data-testid="trades-sync-today"
        >
          Capture today&apos;s trades from Kite
        </Button>
      </div>

      {result === null ? null : result.ok ? (
        <Report report={result.report} />
      ) : (
        <p role="alert" className="text-sm text-destructive" data-testid="trade-import-error">
          {result.error}
        </p>
      )}

      {rows.length > 0 ? (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[36rem] text-sm" data-testid="trade-history-table">
            <caption className="sr-only">Most recent trades</caption>
            <thead>
              <tr className="border-b border-border text-xs text-muted-foreground">
                <th scope="col" className="px-2 py-1.5 text-left font-normal">Date</th>
                <th scope="col" className="px-2 py-1.5 text-left font-normal">Stock</th>
                <th scope="col" className="px-2 py-1.5 text-left font-normal">Side</th>
                <th scope="col" className="px-2 py-1.5 text-right font-normal">Qty</th>
                <th scope="col" className="px-2 py-1.5 text-right font-normal">Price</th>
                <th scope="col" className="px-2 py-1.5 text-right font-normal">Value</th>
                <th scope="col" className="px-2 py-1.5 text-left font-normal">From</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border/60">
              {rows.map((row) => (
                <tr key={`${row.exchange}-${row.trade_id}`}>
                  <td className="px-2 py-1.5 tabular-nums">{formatTradeDate(row.trade_date)}</td>
                  <td className="px-2 py-1.5 font-medium">{row.symbol}</td>
                  <td className="px-2 py-1.5">{row.side === "BUY" ? "Buy" : "Sell"}</td>
                  <td className="px-2 py-1.5 text-right tabular-nums">{formatNumber(row.quantity)}</td>
                  <td className="px-2 py-1.5 text-right tabular-nums">
                    {row.price == null ? EMPTY_CELL : formatNumber(row.price, { decimals: 2 })}
                  </td>
                  <td className="px-2 py-1.5 text-right tabular-nums">{formatMoney(row.value)}</td>
                  <td className="px-2 py-1.5 text-muted-foreground">
                    {row.source === "KITE_API" ? "Kite" : "Tradebook"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </section>
  );
}
