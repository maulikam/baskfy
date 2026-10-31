"use client";

import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
  type SortingState,
} from "@tanstack/react-table";
import { ChevronDown, ChevronRight } from "lucide-react";
import { Fragment, useState } from "react";

import { SortableHeader } from "@/components/opportunities/sortable-header";
import { DataStatusBanner, WarningsList } from "@/components/opportunities/states";
import { SwingDetail } from "@/components/opportunities/swing-detail";
import { Badge } from "@/components/ui/badge";
import { formatNumber } from "@/lib/format";
import type { DataStatus, SwingOpportunity, SwingScanResponse } from "@/lib/opportunities/types";
import {
  SETUP_LABEL,
  actionabilityLabel,
  emptyReason,
  formatInr,
  formatInrCrore,
  formatIstDateTime,
  formatPct,
  formatSessionDate,
  funnelSteps,
  label,
  statusLabel,
} from "@/lib/opportunities/view";

const column = createColumnHelper<SwingOpportunity>();

export function ActionableBadge({
  opportunity,
  status,
}: {
  opportunity: Pick<SwingOpportunity, "actionable">;
  status: DataStatus;
}) {
  const verdict = actionabilityLabel(opportunity, status);
  return (
    <Badge variant={verdict.actionable ? "positive" : "warning"} data-testid="actionable-badge">
      {verdict.label}
    </Badge>
  );
}

function columns(status: DataStatus) {
  return [
    column.display({
      id: "expand",
      header: () => <span className="sr-only">Details</span>,
      cell: ({ row }) => (
        <button
          type="button"
          onClick={row.getToggleExpandedHandler()}
          aria-expanded={row.getIsExpanded()}
          aria-label={`${row.getIsExpanded() ? "Hide" : "Show"} details for ${row.original.symbol}`}
          className="rounded p-0.5 hover:bg-muted"
        >
          {row.getIsExpanded() ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}
        </button>
      ),
    }),
    column.accessor("symbol", {
      header: "Symbol",
      cell: ({ row }) => (
        <span className="flex flex-col">
          <span className="font-medium">{row.original.symbol}</span>
          <span className="max-w-[14rem] truncate text-xs text-muted-foreground">{row.original.name}</span>
        </span>
      ),
    }),
    column.accessor("setup", { header: "Setup", cell: (c) => label(SETUP_LABEL, c.getValue()) }),
    column.accessor("status", {
      header: "Status",
      cell: ({ row }) => (
        <span className="flex flex-col items-start gap-1">
          <Badge variant={row.original.status === "TRIGGERED" ? "accent" : "outline"}>
            {statusLabel(row.original.status)}
          </Badge>
          <ActionableBadge opportunity={row.original} status={status} />
        </span>
      ),
    }),
    column.accessor((r) => r.score.total, {
      id: "score",
      header: "Score",
      cell: (c) => formatNumber(c.getValue(), { decimals: 1 }),
    }),
    column.accessor((r) => r.entry.price, { id: "entry", header: "Entry", cell: (c) => formatInr(c.getValue()) }),
    column.accessor("invalidation", { header: "Invalidation", cell: (c) => formatInr(c.getValue()) }),
    column.accessor((r) => r.targets[0]?.price ?? null, {
      id: "targets",
      header: "Target(s)",
      sortUndefined: "last",
      cell: ({ row }) =>
        row.original.targets.length === 0
          ? "—"
          : row.original.targets.map((t) => formatInr(t.price)).join(" / "),
    }),
    column.accessor("risk_reward", { header: "R:R", cell: (c) => formatNumber(c.getValue(), { decimals: 2 }) }),
    column.accessor((r) => r.volatility.atr_pct, { id: "atr", header: "ATR%", cell: (c) => formatPct(c.getValue()) }),
    column.accessor((r) => r.liquidity.avg_turnover_cr_20d, {
      id: "turnover",
      header: "Turnover 20d",
      cell: (c) => formatInrCrore(c.getValue()),
    }),
    column.accessor((r) => r.relative_strength.excess_return_pct ?? Number.NEGATIVE_INFINITY, {
      id: "rs",
      header: "RS excess",
      cell: ({ row }) => formatPct(row.original.relative_strength.excess_return_pct, 2, true),
    }),
  ];
}

export function SwingResultsTable({
  response,
  horizonMax,
}: {
  response: SwingScanResponse;
  horizonMax: number;
}) {
  const [sorting, setSorting] = useState<SortingState>([{ id: "score", desc: true }]);
  const [expanded, setExpanded] = useState({});
  const table = useReactTable({
    data: response.opportunities,
    columns: columns(response.data_status),
    state: { sorting, expanded },
    onSortingChange: setSorting,
    onExpandedChange: setExpanded,
    getRowId: (row) => row.id,
    getRowCanExpand: () => true,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  });
  const span = table.getVisibleLeafColumns().length;

  return (
    <div className="overflow-x-auto" data-testid="swing-results">
      <table className="w-full min-w-[68rem] border-collapse text-sm">
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
            <Fragment key={row.id}>
              <tr className="border-b border-border/40 align-top">
                {row.getVisibleCells().map((cell) => (
                  <td key={cell.id} className="py-2 pr-3 tabular-nums">
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
                  </td>
                ))}
              </tr>
              {row.getIsExpanded() ? (
                <tr className="border-b border-border/40 bg-muted/30">
                  <td colSpan={span}>
                    <SwingDetail opportunity={row.original} universe={response.universe} horizonMax={horizonMax} />
                  </td>
                </tr>
              ) : null}
            </Fragment>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function SwingResultView({
  response,
  horizonMax,
}: {
  response: SwingScanResponse;
  horizonMax: number;
}) {
  return (
    <div className="space-y-4">
      <p className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground" data-testid="swing-timestamps">
        <span>Data as of session {formatSessionDate(response.data_as_of)}</span>
        <span>Scanned at {formatIstDateTime(response.scanned_at)}</span>
        <span>
          {formatNumber(response.counts.qualified)} qualified · {formatNumber(response.counts.pending)} pending ·{" "}
          {formatNumber(response.counts.triggered)} triggered
        </span>
      </p>
      <DataStatusBanner status={response.data_status} reason={response.stale_reason} />
      <WarningsList warnings={response.warnings} />
      {response.data_status === "UNAVAILABLE" ? null : response.opportunities.length === 0 ? (
        <div className="space-y-3 rounded-md border border-border/70 p-4" data-testid="swing-empty">
          <p className="text-sm">No opportunities matched. {emptyReason(response.counts)}</p>
          <ol className="flex flex-wrap gap-2 text-xs">
            {funnelSteps(response.counts).map((step) => (
              <li key={step.label} className="rounded border border-border/60 px-2 py-1">
                {step.label}: <span className="font-medium tabular-nums">{formatNumber(step.value)}</span>
              </li>
            ))}
          </ol>
        </div>
      ) : (
        <SwingResultsTable response={response} horizonMax={horizonMax} />
      )}
    </div>
  );
}
