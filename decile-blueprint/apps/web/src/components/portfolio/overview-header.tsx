"use client";

import { Download, Eye, EyeOff, Plus, RefreshCw } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";

import { syncHoldingsAction } from "@/app/actions/brokers";
import { useAmounts } from "@/components/portfolio/amounts";
import { PageHeader } from "@/components/shell/page-header";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { formatTradeDate } from "@/lib/format";
import {
  describeBrokers,
  pricesLine,
  syncedLine,
  type Overview,
  type PortfolioRow,
  type SyncStatus,
} from "@/lib/portfolio/overview";

/**
 * §6.1 — the header of the single screen.
 *
 * ## The two timestamps, and why they are never one line
 *
 * §6.1: *"Timestamps shown separately: Prices: close of {date} · Holdings synced: {time}."*
 *
 * They answer different questions and can be days apart. **Prices** is the trading day the
 * valuation used — §5.1 makes v1 end-of-day, so a portfolio valued at Friday's close is still
 * valued at Friday's close on Sunday afternoon. **Holdings synced** is when the broker last told
 * us what is actually held, and that can be five minutes ago on that same Sunday. Merging them
 * into one "as of" line makes a stale price look fresh because the sync ran, or a fresh price
 * look stale because a token expired; either direction misleads the reader about which half of
 * the number to distrust. Two `<span>`s, two testids, one separator that is `aria-hidden`.
 *
 * Neither is invented when missing: the API's own sentence ("No closing prices yet", "Holdings
 * not synced yet") is printed instead of a date borrowed from the other fact.
 */

/** Everything the reader can see, flattened for §6.1's Export control. */
export function csvFor(rows: readonly PortfolioRow[]): string {
  const header =
    "name,kind,counts_toward_total,source,publisher,value,cash,todays_pnl,return_label,return_value,return_since,brokers,status\n";
  const escape = (text: string): string =>
    /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  const body = rows
    .map((row) =>
      [
        escape(row.name),
        row.kind,
        String(row.counts_toward_total),
        row.source,
        escape(row.publisher ?? ""),
        row.value,
        row.cash,
        row.todays_pnl.amount ?? "",
        escape(row.headline_return.label),
        row.headline_return.value ?? "",
        row.headline_return.since,
        escape(describeBrokers(row)),
        escape(row.status),
      ].join(","),
    )
    .join("\n");
  return `${header}${body}\n`;
}

function syncSummary(sync: readonly SyncStatus[]): string {
  if (sync.length === 0) return "No broker connected";
  if (sync.length === 1) return sync[0]?.broker.label ?? "1 broker";
  return `Connected to ${sync.length} brokers`;
}

function BrokerSyncMenu({ sync }: { sync: readonly SyncStatus[] }) {
  const router = useRouter();
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [syncing, startSync] = useTransition();

  function runSync(brokerId: string) {
    setNote(null);
    setPendingId(brokerId);
    startSync(async () => {
      const result = await syncHoldingsAction(brokerId);
      setPendingId(null);
      setNote(
        result.persisted
          ? `${result.written} holding${result.written === 1 ? "" : "s"} synced.`
          : result.sync_note || result.note || "Could not sync holdings.",
      );
      if (result.persisted) router.refresh();
    });
  }

  return (
    <div className="flex flex-wrap items-center gap-2">
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button type="button" variant="outline" size="sm" data-testid="sync-status">
            <RefreshCw aria-hidden="true" className={syncing ? "animate-spin" : undefined} />
            {syncSummary(sync)}
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-80">
          <DropdownMenuLabel>Broker sync</DropdownMenuLabel>
          {sync.length === 0 ? (
            <p className="px-2 py-1.5 text-xs text-muted-foreground">
              Connect a broker and your holdings appear here. Nothing is bought or sold.{" "}
              <Link href="/brokers" className="text-foreground underline-offset-4 hover:underline">
                Open brokers
              </Link>
            </p>
          ) : (
            <ul className="px-2 py-1 text-xs">
              {sync.map((row) => {
                const brokerId = row.broker.broker_id;
                const busy = syncing && pendingId === brokerId;
                return (
                  <li
                    key={row.broker.broker_account_id}
                    data-testid="broker-sync-row"
                    className="flex items-center justify-between gap-3 py-1.5"
                  >
                    <span className="min-w-0">
                      <span className="block font-medium text-foreground">{row.broker.label}</span>
                      <span className="text-muted-foreground">
                        {row.synced_on === null || row.synced_on === undefined
                          ? row.label
                          : formatTradeDate(row.synced_on)}
                      </span>
                    </span>
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      disabled={syncing}
                      data-testid={`sync-now-${brokerId}`}
                      onClick={() => runSync(brokerId)}
                    >
                      {busy ? "Syncing…" : "Sync now"}
                    </Button>
                  </li>
                );
              })}
            </ul>
          )}
        </DropdownMenuContent>
      </DropdownMenu>
      {note ? (
        <p className="max-w-[16rem] text-xs text-muted-foreground" data-testid="sync-note" role="status">
          {note}
        </p>
      ) : null}
    </div>
  );
}

export interface OverviewHeaderProps {
  overview: Overview;
  /** The rows currently on screen — what Export writes. */
  rows: readonly PortfolioRow[];
}

export function OverviewHeader({ overview, rows }: OverviewHeaderProps) {
  const { visible, toggle } = useAmounts();
  const sync = overview.sync_status ?? [];

  function exportCsv() {
    const blob = new Blob([csvFor(rows)], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `baskfy-portfolio-${overview.prices_as_of ?? "latest"}.csv`;
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
  }

  return (
    <PageHeader
      title="My Portfolio"
      blurb="Your complete investment picture across baskets and brokers."
      actions={
        <>
          <Button
            type="button"
            variant="outline"
            size="sm"
            aria-pressed={!visible}
            onClick={toggle}
            data-testid="toggle-amounts"
          >
            {visible ? <EyeOff aria-hidden="true" /> : <Eye aria-hidden="true" />}
            {visible ? "Hide amounts" : "Show amounts"}
          </Button>

          <BrokerSyncMenu sync={sync} />

          <Button variant="primary" size="sm" asChild>
            <Link href="/portfolio/portfolios" data-testid="new-portfolio">
              <Plus aria-hidden="true" />
              New portfolio
            </Link>
          </Button>

          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={exportCsv}
            disabled={rows.length === 0}
            data-testid="export-portfolio"
          >
            <Download aria-hidden="true" />
            Export
          </Button>
        </>
      }
      meta={
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
          <span data-testid="prices-as-of">{pricesLine(overview)}</span>
          <span aria-hidden="true" className="text-border">
            ·
          </span>
          <span data-testid="holdings-synced">{syncedLine(overview)}</span>
        </div>
      }
    />
  );
}
