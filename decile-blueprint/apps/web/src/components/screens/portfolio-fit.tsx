"use client";

import type {
  PortfolioNodeOut,
  ScreenDefinition,
  ScreenSelectionOut,
  SelectionRowOut,
} from "@baskfy/api-client";
import { Info } from "lucide-react";
import { useId, useMemo, useState } from "react";

import { Disclaimer } from "@/components/data/disclaimer";
import { ErrorState } from "@/components/data/error-state";
import { DraftNumberInput } from "@/components/screens/draft-number-input";
import { ProvenanceHeader } from "@/components/screens/provenance-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { EMPTY_CELL, formatTradeDate } from "@/lib/format";
import { formatQuantity, formatRupees } from "@/lib/portfolios/decimal";
import { usePortfolios } from "@/lib/portfolios/queries";
import { forestNodes } from "@/lib/portfolios/tree";
import {
  EMPTY_CONSTRAINTS,
  FIT_ACTION_LABEL,
  FIT_ACTION_TONE,
  constraintsProblem,
  fitRequest,
  flagLabel,
  reasonLabel,
  type FitConstraintsDraft,
  type FitSource,
} from "@/lib/screens/portfolio-fit";
import { hasRankingTerms, usePortfolioFit } from "@/lib/screens/queries";
import { cn } from "@/lib/utils";

/**
 * Portfolio fit — `POST /screens/selection`, gates/ranking-2.H-web.md G5.
 *
 * How one of your portfolios lines up with this screen's ranking: which names it would keep, add,
 * remove or skip, and why. **Informational only.** It places no orders, saves nothing, and sits
 * apart from the results table: it never reorders the quality ranking, and its rows keep the
 * engine's `quality_rank` verbatim, in the order the server sent them.
 */

/** The label every state of the panel carries. A component, like the disclaimer (house rule 9). */
export function InformationalNotice({ className }: { className?: string | undefined }) {
  return (
    <p
      data-testid="fit-informational"
      className={cn(
        "flex gap-2 rounded-md border border-border bg-muted/50 px-3 py-2 text-xs text-muted-foreground",
        className,
      )}
    >
      <Info aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />
      <span>
        Informational only — no orders. Nothing here is sent to a broker or changes your portfolio,
        and the results above keep their own order.
      </span>
    </p>
  );
}

function SummaryItem({ label, value, testId }: { label: string; value: string; testId: string }) {
  return (
    <div className="flex items-baseline gap-1" data-testid={testId}>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="tabular-nums">{value}</dd>
    </div>
  );
}

function sizeText(row: SelectionRowOut): string {
  if (row.proposed_qty === null) return EMPTY_CELL;
  const shares = `${row.proposed_qty} ${row.proposed_qty === 1 ? "share" : "shares"}`;
  return row.proposed_value_inr === null
    ? shares
    : `${shares} (${formatRupees(row.proposed_value_inr, { decimals: 0 })})`;
}

export interface PortfolioFitResultProps {
  result: ScreenSelectionOut;
  /** The portfolio's name, when the fit was computed over one. */
  portfolioName?: string | undefined;
}

export function PortfolioFitResult({ result, portfolioName }: PortfolioFitResultProps) {
  const { summary, rows } = result;
  const showSize = rows.some((row) => row.proposed_qty !== null);

  return (
    <div className="space-y-4" data-testid="fit-result">
      <p className="text-xs text-muted-foreground">
        {portfolioName ? `${portfolioName}, ` : "Starting from no holdings, "}
        checked against prices as of {formatTradeDate(result.as_of)}.
      </p>

      <ProvenanceHeader provenance={result.provenance} />

      <dl data-testid="fit-summary" className="flex flex-wrap gap-x-4 gap-y-1 text-xs">
        <SummaryItem label="Keep" value={String(summary.kept)} testId="fit-summary-kept" />
        <SummaryItem label="Would add" value={String(summary.entries)} testId="fit-summary-entries" />
        <SummaryItem label="Would remove" value={String(summary.exits)} testId="fit-summary-exits" />
        <SummaryItem label="Skipped" value={String(summary.skips)} testId="fit-summary-skips" />
        <SummaryItem
          label="Empty slots"
          value={String(summary.unfilled_slots)}
          testId="fit-summary-unfilled"
        />
        <SummaryItem
          label="Changes"
          value={
            summary.turnover_budget === null
              ? String(summary.turnover_used)
              : `${summary.turnover_used} of ${summary.turnover_budget}`
          }
          testId="fit-summary-changes"
        />
      </dl>

      {result.holdings_without_quantity.length > 0 ? (
        <div
          role="status"
          data-testid="fit-holdings-without-quantity"
          className="rounded-md border border-warning/40 bg-warning-muted px-3 py-2 text-xs text-warning"
        >
          <p className="font-medium">
            Left out because no quantity is recorded ({result.holdings_without_quantity.length}):
          </p>
          <ul className="mt-1 flex flex-wrap gap-x-3 gap-y-1">
            {result.holdings_without_quantity.map((symbol) => (
              <li key={symbol}>{symbol}</li>
            ))}
          </ul>
          <p className="mt-1">Add a quantity to these holdings to include them.</p>
        </div>
      ) : null}

      {result.notes.length > 0 ? (
        <ul data-testid="fit-notes" className="space-y-1 text-xs text-muted-foreground">
          {result.notes.map((note) => (
            <li key={`${note.code}-${note.message}`}>{note.message}</li>
          ))}
        </ul>
      ) : null}

      {rows.length === 0 ? (
        <p className="text-sm text-muted-foreground" data-testid="fit-empty">
          No names to show: nothing held and nothing ranked inside the add limit.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table data-testid="fit-rows" className="w-full text-sm">
            <caption className="sr-only">Portfolio fit, one row per stock</caption>
            <thead>
              <tr className="text-left text-xs text-muted-foreground">
                <th scope="col" className="py-1 pr-3 font-medium">Stock</th>
                <th scope="col" className="px-2 py-1 text-right font-medium">Quality rank</th>
                <th scope="col" className="px-2 py-1 font-medium">Decision</th>
                <th scope="col" className="px-2 py-1 font-medium">Why</th>
                {showSize ? (
                  <th scope="col" className="py-1 pl-2 text-right font-medium">
                    Illustrative size
                  </th>
                ) : null}
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {rows.map((row) => (
                <tr key={`${row.instrument_id}-${row.action}`} data-testid={`fit-row-${row.symbol}`}>
                  <th scope="row" className="py-2 pr-3 text-left align-top font-medium">
                    {row.symbol}
                    {row.sector ? (
                      <span className="block text-xs font-normal text-muted-foreground">
                        {row.sector}
                      </span>
                    ) : null}
                  </th>
                  <td
                    className="px-2 py-2 text-right align-top tabular-nums"
                    data-testid={`fit-rank-${row.symbol}`}
                  >
                    {row.quality_rank === null ? EMPTY_CELL : `#${row.quality_rank}`}
                  </td>
                  <td className="px-2 py-2 align-top">
                    <Badge variant={FIT_ACTION_TONE[row.action]} data-testid={`fit-action-${row.symbol}`}>
                      {FIT_ACTION_LABEL[row.action]}
                    </Badge>
                  </td>
                  <td className="px-2 py-2 align-top text-xs" data-testid={`fit-reasons-${row.symbol}`}>
                    <ul className="space-y-0.5">
                      {row.reasons.map((reason) => (
                        <li key={reason}>{reasonLabel(reason)}</li>
                      ))}
                    </ul>
                    {row.flags.length > 0 ? (
                      <ul className="mt-1 space-y-0.5 text-warning" data-testid={`fit-flags-${row.symbol}`}>
                        {row.flags.map((flag) => (
                          <li key={flag}>{flagLabel(flag)}</li>
                        ))}
                      </ul>
                    ) : null}
                    {row.explanation ? (
                      <p className="mt-1 text-muted-foreground">{row.explanation}</p>
                    ) : null}
                    {row.current_quantity !== null ? (
                      <p className="mt-1 text-muted-foreground">
                        Held now: {formatQuantity(row.current_quantity)}
                      </p>
                    ) : null}
                  </td>
                  {showSize ? (
                    <td className="py-2 pl-2 text-right align-top tabular-nums">{sizeText(row)}</td>
                  ) : null}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {showSize ? (
        <p className="text-xs text-muted-foreground">
          Illustrative size splits the capital you entered evenly across the slots. It is not a
          recommendation to trade.
        </p>
      ) : null}
    </div>
  );
}

const SOURCE_EMPTY = "empty";

function portfolioOptionLabel(node: PortfolioNodeOut): string {
  const indent = "  ".repeat(node.depth);
  const count = `${node.holdings_count} ${node.holdings_count === 1 ? "holding" : "holdings"}`;
  return `${indent}${node.name} (${count})`;
}

interface NumberFieldProps {
  label: string;
  hint: string;
  value: number | null;
  onValue: (value: number | null) => void;
  placeholder: string;
  step?: string;
}

function NumberField({ label, hint, value, onValue, placeholder, step = "1" }: NumberFieldProps) {
  const id = useId();
  return (
    <div className="space-y-1">
      <Label htmlFor={id} className="text-xs">
        {label}
      </Label>
      <DraftNumberInput
        id={id}
        aria-describedby={`${id}-hint`}
        placeholder={placeholder}
        step={step}
        value={value}
        onValue={onValue}
      />
      <p id={`${id}-hint`} className="text-xs text-muted-foreground">
        {hint}
      </p>
    </div>
  );
}

export interface PortfolioFitPanelProps {
  definition: ScreenDefinition;
  asOf?: string | undefined;
  dataVersion?: number | undefined;
}

/** The picker, the constraints and the answer. Mounted only once the section is opened. */
export function PortfolioFitPanel({ definition, asOf, dataVersion }: PortfolioFitPanelProps) {
  const portfolios = usePortfolios();
  const fit = usePortfolioFit();
  const sourceId = useId();
  const capitalId = useId();
  const [sourceValue, setSourceValue] = useState("");
  const [draft, setDraft] = useState<FitConstraintsDraft>(EMPTY_CONSTRAINTS);
  const [checkedFor, setCheckedFor] = useState<ScreenDefinition | null>(null);

  const nodes = useMemo(() => forestNodes(portfolios.data), [portfolios.data]);
  const ranked = hasRankingTerms(definition);
  const problem = constraintsProblem(draft);

  const source: FitSource | null =
    sourceValue === ""
      ? null
      : sourceValue === SOURCE_EMPTY
        ? { kind: "empty" }
        : { kind: "portfolio", id: Number(sourceValue) };
  const portfolioName =
    source?.kind === "portfolio" ? nodes.find((node) => node.id === source.id)?.name : undefined;

  const set = <K extends keyof FitConstraintsDraft>(key: K, value: FitConstraintsDraft[K]) =>
    setDraft((current) => ({ ...current, [key]: value }));

  const check = () => {
    if (!source || problem !== null || !ranked) return;
    setCheckedFor(definition);
    fit.mutate(fitRequest({ definition, source, constraints: draft, asOf, dataVersion }));
  };

  return (
    <div className="space-y-4" data-testid="fit-panel">
      {ranked ? null : (
        <p className="text-sm text-muted-foreground" data-testid="fit-needs-terms">
          Portfolio fit reads the ranking terms. Add at least one term in Ranking to use it.
        </p>
      )}

      <div className="grid gap-3 sm:grid-cols-2">
        <div className="space-y-1 sm:col-span-2">
          <Label htmlFor={sourceId} className="text-xs">
            Portfolio
          </Label>
          <Select
            id={sourceId}
            value={sourceValue}
            data-testid="fit-portfolio"
            disabled={portfolios.isPending && !portfolios.data}
            onChange={(event) => setSourceValue(event.currentTarget.value)}
          >
            <option value="">
              {portfolios.isPending && !portfolios.data ? "Loading portfolios…" : "Choose a portfolio"}
            </option>
            {nodes.map((node) => (
              <option key={node.id} value={String(node.id)}>
                {portfolioOptionLabel(node)}
              </option>
            ))}
            <option value={SOURCE_EMPTY}>Start from no holdings</option>
          </Select>
          {portfolios.error ? (
            <ErrorState error={portfolios.error} onRetry={() => void portfolios.refetch()} />
          ) : null}
        </div>

        <NumberField
          label="Most stocks"
          hint="How many names the list may hold. Blank uses the default."
          placeholder="Default"
          value={draft.max_names}
          onValue={(value) => set("max_names", value)}
        />
        <NumberField
          label="Add limit"
          hint="Add a stock only if it ranks this high or better. Blank uses the default."
          placeholder="Default"
          value={draft.entry_rank}
          onValue={(value) => set("entry_rank", value)}
        />
        <NumberField
          label="Keep limit"
          hint="Keep a held stock while it ranks this high or better. Blank uses the default."
          placeholder="Default"
          value={draft.retention_rank}
          onValue={(value) => set("retention_rank", value)}
        />
        <NumberField
          label="Most stocks per sector"
          hint="Blank means no sector limit."
          placeholder="No limit"
          value={draft.max_per_sector}
          onValue={(value) => set("max_per_sector", value)}
        />
        <NumberField
          label="Most changes"
          hint="Adds plus removals allowed in one check. Blank means no limit."
          placeholder="No limit"
          value={draft.turnover_budget_names}
          onValue={(value) => set("turnover_budget_names", value)}
        />
        <NumberField
          label="Correlation limit"
          hint="Skip a stock that moves this closely with one already in the list, from -1 to 1. Blank means no limit."
          placeholder="No limit"
          step="0.05"
          value={draft.max_correlation}
          onValue={(value) => set("max_correlation", value)}
        />
        <div className="space-y-1 sm:col-span-2">
          <Label htmlFor={capitalId} className="text-xs">
            Capital, in rupees
          </Label>
          <Input
            id={capitalId}
            inputMode="decimal"
            placeholder="Optional"
            aria-describedby={`${capitalId}-hint`}
            value={draft.capital_inr}
            onChange={(event) => set("capital_inr", event.currentTarget.value)}
          />
          <p id={`${capitalId}-hint`} className="text-xs text-muted-foreground">
            Only used to show an illustrative size and check each stock&rsquo;s daily trading value.
          </p>
        </div>
      </div>

      {problem ? (
        <p role="alert" className="text-xs text-negative" data-testid="fit-constraints-problem">
          {problem}
        </p>
      ) : null}

      <Button
        size="sm"
        variant="secondary"
        data-testid="fit-check"
        disabled={!ranked || source === null || problem !== null || fit.isPending}
        onClick={check}
      >
        {fit.isPending ? "Checking…" : "Check portfolio fit"}
      </Button>

      {fit.error ? <ErrorState error={fit.error} onRetry={check} /> : null}

      {fit.data ? (
        <>
          {checkedFor !== definition ? (
            <p className="text-xs text-warning" data-testid="fit-stale">
              The screen has changed since this check. Check again to update it.
            </p>
          ) : null}
          <PortfolioFitResult result={fit.data} portfolioName={portfolioName} />
        </>
      ) : null}
    </div>
  );
}

export interface PortfolioFitProps extends PortfolioFitPanelProps {
  className?: string | undefined;
}

/**
 * The editor's Portfolio fit section. Closed by default, so opening the screen asks nothing about
 * your portfolios until you do.
 */
export function PortfolioFit({ definition, asOf, dataVersion, className }: PortfolioFitProps) {
  const [open, setOpen] = useState(false);
  const headingId = useId();

  return (
    <section
      aria-labelledby={headingId}
      data-testid="portfolio-fit"
      className={cn("vaaya-card space-y-3 px-4 py-4", className)}
    >
      <div className="flex flex-wrap items-center gap-2">
        <h2 id={headingId} className="text-base font-semibold">
          Portfolio fit
        </h2>
        <Badge variant="neutral">Informational</Badge>
        <Button
          variant="ghost"
          size="sm"
          className="ml-auto"
          aria-expanded={open}
          data-testid="fit-toggle"
          onClick={() => setOpen((current) => !current)}
        >
          {open ? "Hide" : "Open"}
        </Button>
      </div>
      <p className="text-sm font-light text-muted-foreground">
        See which of a portfolio&rsquo;s stocks this screen would keep, add, remove or skip, and
        why. Separate from the ranking above, which it never reorders.
      </p>
      <InformationalNotice />
      {open ? <PortfolioFitPanel definition={definition} asOf={asOf} dataVersion={dataVersion} /> : null}
      <Disclaimer />
    </section>
  );
}
