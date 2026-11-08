"use client";

import { useMemo, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  brokerBreakdown,
  brokersIn,
  describeAllocations,
  describeHolding,
  EMPTY_FILTERS,
  capacityFor,
  filterHoldings,
  formatShares,
  holdingKeyId,
  isOverFree,
  isUnallocated,
  NO_FIGURE,
  NO_FILTER,
  rowKeyIds,
  sectorsIn,
  selectionTotal,
  type AggregatedHolding,
  type HoldingBrokerLine,
  type HoldingFilters,
  type InstrumentRef,
} from "@/lib/portfolio/organize";
import { formatRupees } from "@/lib/portfolios/decimal";
import { cn } from "@/lib/utils";

/**
 * PORTFOLIO_REDESIGN.md §6.7's two-panel picker: consolidated holdings on the left, the portfolio
 * building up on the right with a live total.
 *
 * Three rules from the spec are structural here rather than decorative.
 *
 * **Part quantities (0035, 10 Sep 2026).** This block used to say the opposite — "there is no
 * quantity field anywhere in this component, and there cannot be one" — because v1 allocated a
 * holding whole. Maulik asked for the opposite: *"one stock can appear in multiple portfolios, so
 * if stock a bought 100 qty for shortterm 20 for long term 34 for some swing 36 for momentum"*.
 *
 * So the tick is still the only control on the left — picking is one gesture — and the *amount*
 * is tuned on the right, in the basket, where the user is already looking at what they have
 * chosen. A blank box means "all of it", which keeps the common case a single click and keeps
 * every pre-0035 flow behaving exactly as it did.
 *
 * The box is capped at the leg's **free** shares, not its total: shares already filed into
 * another portfolio are spoken for, and offering them here would produce a refusal from the API
 * that the user could have been shown before they typed.
 *
 * **Aggregated display, per-broker truth (§6.7).** A stock held at two brokers shows as
 * "HDFC Bank — 320 (Zerodha 200 · Upstox 120)". Ticking that row ticks both legs, because that is
 * what the user means; the drill-down underneath lets them tick one leg and not the other, because
 * the ledger keeps the two positions apart and they can be allocated apart. What is never offered
 * is "200 of the 320".
 *
 * **A total that can admit ignorance.** The live total sums only the legs we have a close for and
 * says how many it could not price. A selected holding with no price is not worth zero.
 */

export interface HoldingsPickerProps {
  rows: readonly AggregatedHolding[];
  /** `{ instrument_id -> sector }`. Empty when instrument sectors are not synced. */
  sectors?: Readonly<Record<string, string>> | undefined;
  /** Selected `holdingKeyId`s. */
  selected: ReadonlySet<string>;
  onChange: (next: Set<string>) => void;
  /**
   * `holdingKeyId -> how many shares`, as the user typed it. A missing or blank entry means
   * "all of the free shares", which is what an untouched tick means and what the API's own
   * `quantity: null` means on the wire.
   */
  quantities?: ReadonlyMap<string, string>;
  onQuantitiesChange?: (next: Map<string, string>) => void;
  /**
   * The portfolio being filed INTO, when that portfolio already exists. It changes the ceiling on
   * every box: a new portfolio may only take unallocated shares, while an existing one may move
   * them, so its ceiling is the position less what it already holds. Null while creating.
   */
  targetPortfolioId?: number | null;
  /** The name typed so far, shown as the right panel's heading. */
  portfolioName: string;
}

export function HoldingsPicker({
  rows,
  sectors = {},
  selected,
  onChange,
  quantities = new Map(),
  onQuantitiesChange,
  targetPortfolioId = null,
  portfolioName,
}: HoldingsPickerProps) {
  const [filters, setFilters] = useState<HoldingFilters>(EMPTY_FILTERS);

  const brokers = useMemo(() => brokersIn(rows), [rows]);
  const sectorNames = useMemo(() => sectorsIn(rows, sectors), [rows, sectors]);
  const visible = useMemo(() => filterHoldings(rows, filters, sectors), [rows, filters, sectors]);
  const total = useMemo(
    () => selectionTotal(rows, selected, quantities, targetPortfolioId),
    [rows, selected, quantities, targetPortfolioId],
  );
  /** `holdingKeyId -> the leg`, so the basket can show each one's free shares without re-walking. */
  const legsById = useMemo(() => {
    const found = new Map<string, { line: HoldingBrokerLine; instrument: InstrumentRef }>();
    for (const row of rows) {
      for (const line of row.brokers ?? []) {
        const id = holdingKeyId({
          instrument_id: row.instrument.instrument_id,
          broker_account_id: line.broker.broker_account_id,
        });
        found.set(id, { line, instrument: row.instrument });
      }
    }
    return found;
  }, [rows]);
  const selectedRows = useMemo(
    () => rows.filter((row) => rowKeyIds(row).some((id) => selected.has(id))),
    [rows, selected],
  );

  function setKeys(ids: readonly string[], on: boolean): void {
    const next = new Set(selected);
    for (const id of ids) {
      if (on) next.add(id);
      else next.delete(id);
    }
    onChange(next);
    // Un-ticking forgets the amount that was typed. Keeping it would mean a leg re-ticked later
    // silently carries a number the user set for a different portfolio in a different session.
    if (!on && onQuantitiesChange) {
      const kept = new Map(quantities);
      let changed = false;
      for (const id of ids) changed = kept.delete(id) || changed;
      if (changed) onQuantitiesChange(kept);
    }
  }

  function setQuantity(id: string, raw: string): void {
    if (!onQuantitiesChange) return;
    const next = new Map(quantities);
    if (raw.trim() === "") next.delete(id);
    else next.set(id, raw);
    onQuantitiesChange(next);
  }

  function selectAllUnallocated(): void {
    const next = new Set(selected);
    for (const row of rows) {
      if (!isUnallocated(row)) continue;
      for (const id of rowKeyIds(row)) next.add(id);
    }
    onChange(next);
  }

  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)]" data-testid="holdings-picker">
      {/* ---------------------------------------------------------------- left */}
      <section aria-label="Your holdings" className="space-y-3 rounded-xl border border-border bg-card p-4">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h3 className="text-sm font-semibold">Your holdings</h3>
          <Button type="button" variant="outline" size="sm" onClick={selectAllUnallocated}>
            Select all unallocated
          </Button>
        </div>

        <div className="grid gap-2 sm:grid-cols-3">
          <label className="flex flex-col gap-1 text-xs text-muted-foreground">
            Broker
            <select
              value={filters.broker}
              onChange={(event) => setFilters({ ...filters, broker: event.target.value })}
              className="h-8 rounded-sm border border-input bg-background px-2 text-sm text-foreground"
            >
              <option value={NO_FILTER}>All brokers</option>
              {brokers.map((broker) => (
                <option key={broker.broker_account_id} value={String(broker.broker_account_id)}>
                  {broker.label}
                </option>
              ))}
            </select>
          </label>

          <label className="flex flex-col gap-1 text-xs text-muted-foreground">
            Sector
            <select
              value={filters.sector}
              disabled={sectorNames.length === 0}
              onChange={(event) => setFilters({ ...filters, sector: event.target.value })}
              className="h-8 rounded-sm border border-input bg-background px-2 text-sm text-foreground disabled:opacity-50"
            >
              <option value={NO_FILTER}>
                {sectorNames.length === 0 ? "Sectors not synced yet" : "All sectors"}
              </option>
              {sectorNames.map((sector) => (
                <option key={sector} value={sector}>
                  {sector}
                </option>
              ))}
            </select>
          </label>

          <label className="flex flex-col gap-1 text-xs text-muted-foreground">
            Stock
            <input
              type="search"
              value={filters.query}
              placeholder="Symbol or name"
              onChange={(event) => setFilters({ ...filters, query: event.target.value })}
              className="h-8 rounded-sm border border-input bg-background px-2 text-sm text-foreground"
            />
          </label>
        </div>

        {visible.length === 0 ? (
          <p className="rounded-lg border border-dashed border-border px-3 py-6 text-center text-sm text-muted-foreground">
            {rows.length === 0
              ? "No holdings to pick from yet."
              : "No holdings match these filters."}
          </p>
        ) : (
          <ul className="divide-y divide-border/70">
            {visible.map((row) => {
              const ids = rowKeyIds(row);
              const allOn = ids.length > 0 && ids.every((id) => selected.has(id));
              const someOn = !allOn && ids.some((id) => selected.has(id));
              const sector = sectors[String(row.instrument.instrument_id)];
              const lines = row.brokers ?? [];
              return (
                <li key={row.instrument.instrument_id} className="py-2">
                  <div className="flex items-start gap-3">
                    <input
                      type="checkbox"
                      checked={allOn}
                      ref={(node) => {
                        if (node) node.indeterminate = someOn;
                      }}
                      onChange={(event) => setKeys(ids, event.target.checked)}
                      aria-label={`Add ${row.instrument.name} to this portfolio`}
                      className="mt-1 size-4"
                    />
                    <div className="min-w-0 flex-1">
                      <p className="text-sm font-medium">{describeHolding(row)}</p>
                      <p className="text-xs text-muted-foreground">
                        {row.instrument.symbol}
                        {sector === undefined ? "" : ` · ${sector}`}
                        {describeAllocations(row)}
                      </p>
                    </div>
                    <p className="shrink-0 text-sm tabular-nums">
                      {row.value === null || row.value === undefined ? (
                        <span title="No close on record for this instrument" className="text-muted-foreground">
                          {NO_FIGURE}
                        </span>
                      ) : (
                        formatRupees(row.value)
                      )}
                    </p>
                  </div>

                  {lines.length > 1 ? (
                    <details className="ml-7 mt-1">
                      <summary className="cursor-pointer text-xs text-muted-foreground">
                        Split across {lines.length} brokers — pick one
                      </summary>
                      <ul className="mt-1 space-y-1">
                        {lines.map((line) => {
                          const id = holdingKeyId({
                            instrument_id: row.instrument.instrument_id,
                            broker_account_id: line.broker.broker_account_id,
                          });
                          return (
                            <li key={id} className="flex items-center gap-2 text-xs">
                              <input
                                type="checkbox"
                                checked={selected.has(id)}
                                onChange={(event) => setKeys([id], event.target.checked)}
                                aria-label={`Add ${row.instrument.name} at ${line.broker.label} to this portfolio`}
                                className="size-3.5"
                              />
                              <span>
                                {line.broker.label} — {formatShares(line.quantity)}
                              </span>
                              <span className="ml-auto tabular-nums text-muted-foreground">
                                {line.value === null || line.value === undefined
                                  ? NO_FIGURE
                                  : formatRupees(line.value)}
                              </span>
                            </li>
                          );
                        })}
                      </ul>
                    </details>
                  ) : null}
                </li>
              );
            })}
          </ul>
        )}
      </section>

      {/* --------------------------------------------------------------- right */}
      <section
        aria-label="New portfolio"
        data-testid="picker-basket"
        className="flex flex-col gap-3 self-start rounded-xl border border-border bg-muted/30 p-4"
      >
        <div>
          <h3 className="text-sm font-semibold">
            {portfolioName.trim() === "" ? "New portfolio" : portfolioName.trim()}
          </h3>
          <p className="text-xs text-muted-foreground">
            {targetPortfolioId === null
              ? "Leave a quantity blank to file the whole holding."
              : "Leave a quantity blank to move the whole holding here. Unallocated shares are taken first."}
          </p>
        </div>

        <div className="rounded-lg border border-border bg-background p-3">
          <p className="text-xs uppercase tracking-wide text-muted-foreground">Running total</p>
          <p className="text-2xl font-semibold tabular-nums" data-testid="picker-total">
            {total.holdings === 0 ? NO_FIGURE : formatRupees(total.value)}
          </p>
          <p className="text-xs text-muted-foreground" data-testid="picker-count">
            {total.holdings} holding{total.holdings === 1 ? "" : "s"} · {total.instruments} stock
            {total.instruments === 1 ? "" : "s"}
          </p>
          {total.unpriced > 0 ? (
            <p className="mt-1 text-xs text-warning" data-testid="picker-unpriced">
              {total.unpriced} not in the total — no close on record for{" "}
              {total.unpriced === 1 ? "it" : "them"}.
            </p>
          ) : null}
          {total.holdings === 0 ? (
            <p className="mt-1 text-xs text-muted-foreground">
              Nothing picked yet, so there is no figure to show.
            </p>
          ) : null}
        </div>

        {selectedRows.length > 0 ? (
          <ul className="space-y-2" data-testid="picker-selected">
            {selectedRows.map((row) => {
              const ids = rowKeyIds(row);
              const chosen = ids.filter((id) => selected.has(id));
              const breakdown = brokerBreakdown(row);
              return (
                <li key={row.instrument.instrument_id} className="space-y-1 text-sm">
                  <div className="flex items-baseline gap-2">
                    <span className="min-w-0 flex-1 truncate">
                      {row.instrument.name}
                      {chosen.length < ids.length ? (
                        <span className="text-xs text-muted-foreground">
                          {" "}
                          ({chosen.length} of {ids.length} brokers)
                        </span>
                      ) : breakdown === null ? null : (
                        <span className="text-xs text-muted-foreground"> ({breakdown})</span>
                      )}
                    </span>
                    <button
                      type="button"
                      onClick={() => setKeys(ids, false)}
                      className="shrink-0 text-xs text-muted-foreground underline-offset-4 hover:underline"
                    >
                      Remove
                    </button>
                  </div>

                  {/* One box per chosen leg. The cap is the leg's FREE shares, so the control
                      cannot express a request the API would refuse. */}
                  {chosen.map((id) => {
                    const leg = legsById.get(id);
                    if (leg === undefined) return null;
                    const free = capacityFor(leg.line, targetPortfolioId);
                    const typed = quantities.get(id) ?? "";
                    const tooMany = isOverFree(typed, free);
                    return (
                      <div key={id} className="flex items-center gap-2 pl-1 text-xs">
                        {ids.length > 1 ? (
                          <span className="w-20 shrink-0 truncate text-muted-foreground">
                            {leg.line.broker.label}
                          </span>
                        ) : null}
                        <input
                          type="text"
                          inputMode="decimal"
                          value={typed}
                          placeholder={formatShares(free)}
                          onChange={(event) => setQuantity(id, event.target.value)}
                          aria-label={`Shares of ${leg.instrument.name} at ${leg.line.broker.label} for this portfolio`}
                          aria-invalid={tooMany || undefined}
                          className={cn(
                            "h-7 w-24 rounded-sm border bg-background px-2 text-right tabular-nums",
                            tooMany ? "border-destructive text-destructive" : "border-input",
                          )}
                          data-testid={`picker-quantity-${id}`}
                        />
                        <span className="text-muted-foreground">
                          of {formatShares(free)} {targetPortfolioId === null ? "free" : "available"}
                        </span>
                        {tooMany ? (
                          <span className="text-destructive" data-testid={`picker-over-${id}`}>
                            more than you have
                          </span>
                        ) : null}
                      </div>
                    );
                  })}
                </li>
              );
            })}
          </ul>
        ) : null}

        <Badge variant="neutral" className={cn("self-start")}>
          Read-only — nothing here places an order
        </Badge>
      </section>
    </div>
  );
}
