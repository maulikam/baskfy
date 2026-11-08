import type { FactsheetOut } from "@baskfy/api-client";

import { CellList } from "@/components/instrument/cell-list";
import { CorporateActionsTable } from "@/components/instrument/corporate-actions";
import { FactorGrid } from "@/components/instrument/factor-grid";
import { InstrumentHeading } from "@/components/instrument/instrument-header";
import { MarketQuality } from "@/components/instrument/market-quality";
import { MetricCards } from "@/components/instrument/metric-cards";
import { ProsCons } from "@/components/instrument/pros-cons";
import { Section } from "@/components/instrument/section";
import { formatTradeDate } from "@/lib/format";

/**
 * The instrument factsheet — docs/01 §5's eleven blocks, in the reference product's order.
 *
 * docs/08 §"Instrument factsheet" opens with "Follow the teardown's block order", so the order
 * here is not a layout preference: blocks 6–9 are merged into one grid because docs/08 asks for
 * exactly that ("Returns/Sharpe/Volatility/RSI render as a compact 5-column grid, one row per
 * family"), and nothing else is reordered or omitted.
 *
 * Every value comes from the API already rounded (CLAUDE.md house rule 8) and every `null`
 * renders as an em dash rather than a zero — a stock with four months of history has no 1-year
 * Sharpe, and `0.00` would be a statement about it that is false.
 *
 * No `<Disclaimer/>` here: `AppShell` renders one into every page of the application frame, and a
 * second copy of the same regulatory sentence on the same screen reads as a bug rather than as
 * emphasis. CLAUDE.md house rule 9 is satisfied by the frame — see `app-shell.tsx`.
 */
export interface FactsheetProps {
  sheet: FactsheetOut;
  /** Key → sparkline series for the metric cards, fetched alongside the sheet. */
  series: Readonly<Record<string, readonly number[]>>;
}

const WINDOWS = ["1Y", "9M", "6M", "3M", "1M"] as const;

export function Factsheet({ sheet, series }: FactsheetProps) {
  const asOf = formatTradeDate(sheet.as_of);

  return (
    <div className="flex flex-col gap-4">
      <InstrumentHeading
        header={sheet.header}
        memberships={sheet.index_memberships}
        asOf={sheet.as_of}
      />

      {sheet.notes && sheet.notes.length > 0 ? (
        <ul aria-label="Data notes" className="flex flex-col gap-1">
          {sheet.notes.map((note) => (
            <li
              key={note}
              className="rounded-md border border-warning/30 bg-warning-muted px-3 py-1.5 text-xs text-foreground"
            >
              {note}
            </li>
          ))}
        </ul>
      ) : null}

      <Section id="key-stats" title="Key stats">
        <CellList cells={sheet.key_stats} columns={4} />
      </Section>

      <Section
        id="pros-cons"
        title="Pros and cons"
        description="Boolean rules over this instrument's factor row, rendered as sentences."
      >
        <ProsCons
          pros={sheet.pros}
          cons={sheet.cons}
          undecidedCount={sheet.undecided.length}
        />
      </Section>

      <Section
        id="metrics"
        title="Metrics"
        description="Each median is this instrument's own history, not the universe's."
      >
        <MetricCards cards={sheet.metric_cards} series={series} />
      </Section>

      <Section id="price-mas" title="Price & moving averages">
        <CellList cells={sheet.price_and_mas} columns={3} />
      </Section>

      <Section
        id="factors"
        title="Returns, Sharpe, volatility and RSI"
        description={`Across the five windows, as of ${asOf}.`}
      >
        <FactorGrid
          returns={sheet.returns}
          sharpeReturns={sheet.sharpe_returns}
          volatility={sheet.volatility}
          rsi={sheet.rsi}
          universe={sheet.percentile_universe}
          asOf={asOf}
        />
      </Section>

      <Section id="market-quality" title="Market quality">
        <MarketQuality quality={sheet.market_quality} windows={WINDOWS} />
      </Section>

      <Section id="corporate-actions" title="Corporate actions">
        <CorporateActionsTable actions={sheet.corporate_actions} />
      </Section>
    </div>
  );
}
