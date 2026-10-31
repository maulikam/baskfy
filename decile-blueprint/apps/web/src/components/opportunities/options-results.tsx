"use client";

import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
  type SortingState,
} from "@tanstack/react-table";
import { useState } from "react";

import { PayoffChart } from "@/components/opportunities/payoff-chart";
import { SortableHeader } from "@/components/opportunities/sortable-header";
import { DataStatusBanner, ProviderUnavailable, WarningsList } from "@/components/opportunities/states";
import { ScoreBreakdown } from "@/components/opportunities/swing-detail";
import { ActionableBadge } from "@/components/opportunities/swing-results";
import { formatNumber } from "@/lib/format";
import type { OptionCandidate, OptionsAnalysis } from "@/lib/opportunities/types";
import {
  STRATEGY_LABEL,
  formatInr,
  formatIstDateTime,
  formatMaxProfit,
  formatSessionDate,
  label,
  legsSummary,
} from "@/lib/opportunities/view";

const MAX_SELECTED = 3;
const column = createColumnHelper<OptionCandidate>();

function PerLot({ lot, unit }: { lot: number | null; unit: number | null }) {
  return (
    <span className="flex flex-col">
      <span>{formatMaxProfit(lot)}</span>
      <span className="text-xs text-muted-foreground">{lot === null ? "per lot" : `${formatMaxProfit(unit)}/unit`}</span>
    </span>
  );
}

function buildColumns(selected: readonly string[], toggle: (id: string) => void, status: OptionsAnalysis["data_status"]) {
  return [
    column.display({
      id: "select",
      header: () => <span className="sr-only">Compare</span>,
      cell: ({ row }) => {
        const checked = selected.includes(row.original.id);
        return (
          <input
            type="checkbox"
            className="size-4"
            aria-label={`Compare ${label(STRATEGY_LABEL, row.original.strategy)} ${row.original.expiry}`}
            checked={checked}
            disabled={!checked && selected.length >= MAX_SELECTED}
            onChange={() => toggle(row.original.id)}
          />
        );
      },
    }),
    column.accessor("strategy", {
      header: "Strategy",
      cell: ({ row }) => (
        <span className="flex flex-col items-start gap-1">
          <span className="font-medium">{label(STRATEGY_LABEL, row.original.strategy)}</span>
          <ActionableBadge opportunity={row.original} status={status} />
        </span>
      ),
    }),
    column.accessor("expiry", { header: "Expiry", cell: (c) => formatSessionDate(c.getValue()) }),
    column.accessor("dte", { header: "DTE" }),
    column.display({
      id: "legs",
      header: "Legs",
      cell: ({ row }) => (
        <span className="flex flex-col text-xs">
          <span>{legsSummary(row.original.legs)}</span>
          <span className="text-muted-foreground">
            {row.original.lots} lot × {formatNumber(row.original.lot_size)}
          </span>
        </span>
      ),
    }),
    column.accessor("net_debit_per_lot", {
      header: "Net debit/lot",
      cell: ({ row }) => <PerLot lot={row.original.net_debit_per_lot} unit={row.original.net_debit_per_unit} />,
    }),
    column.accessor("max_loss_per_lot", {
      header: "Max loss/lot",
      cell: ({ row }) => <PerLot lot={row.original.max_loss_per_lot} unit={row.original.max_loss_per_unit} />,
    }),
    column.accessor((r) => r.max_profit_per_lot ?? Number.POSITIVE_INFINITY, {
      id: "max_profit",
      header: "Max profit/lot",
      cell: ({ row }) => <PerLot lot={row.original.max_profit_per_lot} unit={row.original.max_profit_per_unit} />,
    }),
    column.display({
      id: "breakevens",
      header: "Breakevens",
      cell: ({ row }) => row.original.breakevens.map((b) => formatNumber(b, { decimals: 2 })).join(", ") || "—",
    }),
    column.accessor((r) => r.costs.round_trip_inr, {
      id: "costs",
      header: "Costs",
      cell: ({ row }) => (
        <span className="flex flex-col">
          <span>{formatInr(row.original.costs.round_trip_inr)}</span>
          <span className="text-xs text-muted-foreground">
            entry {formatInr(row.original.costs.entry_inr)} · max loss incl. costs{" "}
            {formatInr(row.original.max_loss_after_costs_per_lot)}
          </span>
        </span>
      ),
    }),
    column.accessor((r) => r.score.total, {
      id: "score",
      header: "Score",
      cell: (c) => formatNumber(c.getValue(), { decimals: 1 }),
    }),
  ];
}

function LegsDetail({ candidate, modelNote }: { candidate: OptionCandidate; modelNote: string }) {
  const modelled = candidate.legs.some((leg) => leg.greeks_source);
  return (
    <div className="space-y-2">
      <table className="w-full text-xs">
        <thead>
          <tr className="text-left text-muted-foreground">
            <th className="py-1 pr-2 font-medium">Contract</th>
            <th className="py-1 pr-2 font-medium">Side</th>
            <th className="py-1 pr-2 text-right font-medium">Bid / Ask</th>
            <th className="py-1 pr-2 text-right font-medium">Exec</th>
            <th className="py-1 pr-2 text-right font-medium">Spread</th>
            <th className="py-1 pr-2 text-right font-medium">Quote age</th>
            <th className="py-1 pr-2 text-right font-medium">OI / Vol</th>
            <th className="py-1 pr-2 text-right font-medium">IV{modelled ? " (model-derived, Black-76)" : ""}</th>
            <th className="py-1 text-right font-medium">Delta{modelled ? " (model-derived, Black-76)" : ""}</th>
          </tr>
        </thead>
        <tbody>
          {candidate.legs.map((leg) => (
            <tr key={leg.tradingsymbol} className="border-t border-border/40 tabular-nums">
              <td className="py-1 pr-2 font-mono">{leg.tradingsymbol}</td>
              <td className="py-1 pr-2">{leg.side}</td>
              <td className="py-1 pr-2 text-right">
                {formatInr(leg.bid)} / {formatInr(leg.ask)}
              </td>
              <td className="py-1 pr-2 text-right">{formatInr(leg.exec_price)}</td>
              <td className="py-1 pr-2 text-right">{formatNumber(leg.spread_pct, { decimals: 2, suffix: "%" })}</td>
              <td className="py-1 pr-2 text-right">{formatNumber(leg.quote_age_s, { decimals: 0, suffix: " s" })}</td>
              <td className="py-1 pr-2 text-right">
                {formatNumber(leg.oi)} / {formatNumber(leg.volume)}
              </td>
              <td className="py-1 pr-2 text-right">
                {leg.iv === null ? "—" : formatNumber(leg.iv, { decimals: 2, suffix: "%" })}
              </td>
              <td className="py-1 text-right">{leg.delta === null ? "—" : formatNumber(leg.delta, { decimals: 3 })}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {modelNote ? <p className="text-xs text-muted-foreground">{modelNote}</p> : null}
    </div>
  );
}

export function OptionsTable({ analysis }: { analysis: OptionsAnalysis }) {
  const [sorting, setSorting] = useState<SortingState>([{ id: "score", desc: true }]);
  const [selected, setSelected] = useState<string[]>(() => analysis.candidates.slice(0, 1).map((c) => c.id));
  const toggle = (id: string) =>
    setSelected((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : prev.length >= MAX_SELECTED ? prev : [...prev, id],
    );
  const table = useReactTable({
    data: analysis.candidates,
    columns: buildColumns(selected, toggle, analysis.data_status),
    state: { sorting },
    onSortingChange: setSorting,
    getRowId: (row) => row.id,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  });
  const chosen = analysis.candidates.filter((c) => selected.includes(c.id));

  return (
    <div className="space-y-5">
      <div className="overflow-x-auto" data-testid="options-results">
        <table className="w-full min-w-[72rem] border-collapse text-sm">
          <thead>
            {table.getHeaderGroups().map((group) => (
              <tr key={group.id} className="border-b border-border/70">
                {group.headers.map((header) => (
                  <SortableHeader key={header.id} header={header} />
                ))}
              </tr>
            ))}
          </thead>
          <tbody>
            {table.getRowModel().rows.map((row) => (
              <tr key={row.id} className="border-b border-border/40 align-top">
                {row.getVisibleCells().map((cell) => (
                  <td key={cell.id} className="py-2 pr-3 tabular-nums">
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="text-xs text-muted-foreground">Select up to {MAX_SELECTED} candidates to overlay their expiry payoff.</p>
      <PayoffChart candidates={chosen} spot={analysis.underlying.spot} />
      {chosen.map((candidate) => (
        <section
          key={candidate.id}
          aria-label={`${label(STRATEGY_LABEL, candidate.strategy)} details`}
          className="grid gap-4 rounded-md border border-border/60 p-3 lg:grid-cols-2"
        >
          <div className="space-y-2">
            <h4 className="text-sm font-medium">
              {label(STRATEGY_LABEL, candidate.strategy)} · {formatSessionDate(candidate.expiry)}
            </h4>
            <LegsDetail candidate={candidate} modelNote={analysis.model_note} />
            {candidate.limitations.length > 0 ? (
              <ul className="list-disc pl-4 text-xs text-muted-foreground">
                {candidate.limitations.map((l) => (
                  <li key={l}>{l}</li>
                ))}
              </ul>
            ) : null}
          </div>
          <ScoreBreakdown score={candidate.score} />
        </section>
      ))}
    </div>
  );
}

export function OptionsResultView({ analysis }: { analysis: OptionsAnalysis }) {
  if (!analysis.provider.available) return <ProviderUnavailable provider={analysis.provider} />;
  return (
    <div className="space-y-4">
      <p className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground" data-testid="options-timestamps">
        <span>
          {analysis.underlying.symbol} spot{" "}
          {analysis.underlying.spot === null ? "—" : formatNumber(analysis.underlying.spot, { decimals: 2 })}
          {analysis.underlying.spot_as_of ? ` as of ${formatIstDateTime(analysis.underlying.spot_as_of)}` : ""}
        </span>
        <span>Analyzed at {formatIstDateTime(analysis.analyzed_at)}</span>
      </p>
      <DataStatusBanner status={analysis.data_status} reason={null} />
      <WarningsList warnings={analysis.warnings} />
      {analysis.candidates.length === 0 ? (
        <p className="rounded-md border border-border/70 p-4 text-sm" data-testid="options-empty">
          No candidate passed the quote rules and loss limit. {analysis.rejected.length} contracts were rejected — see the
          reasons below.
        </p>
      ) : (
        <OptionsTable key={analysis.analyzed_at} analysis={analysis} />
      )}
      {analysis.rejected.length > 0 ? (
        <details className="rounded-md border border-border/60 p-3 text-sm">
          <summary className="cursor-pointer font-medium">Rejected contracts ({analysis.rejected.length})</summary>
          <ul className="mt-2 space-y-0.5 text-xs">
            {analysis.rejected.map((r, i) => (
              <li key={`${r.tradingsymbol}-${i}`}>
                <span className="font-mono">{r.tradingsymbol}</span> — {r.reason}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </div>
  );
}
