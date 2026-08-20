"use client";

import type { ColumnDef } from "@tanstack/react-table";
import { useMemo, useState } from "react";

import { DEMO_FACTORS, makeRows, makeSeries, type DemoRow } from "@/app/(app)/kitchen-sink/fixtures";
import { DataTable, type Density } from "@/components/data/data-table";
import { Disclaimer } from "@/components/data/disclaimer";
import { EmptyState } from "@/components/data/empty-state";
import { ErrorState } from "@/components/data/error-state";
import { FactorCombobox } from "@/components/data/factor-combobox";
import { FilterAccordion } from "@/components/data/filter-accordion";
import { MetricGrid } from "@/components/data/metric-grid";
import { SentinelNumberInput } from "@/components/data/sentinel-number-input";
import { Sparkline } from "@/components/data/sparkline";
import { StatCard } from "@/components/data/stat-card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import {
  DEFAULT_REPEAT_HEADER_EVERY,
} from "@/components/data/data-table";
import {
  formatCrore,
  formatFraction,
  formatNumber,
  formatPercent,
} from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * Every core primitive of Prompt 8 deliverable 3, on one page, in the real shell.
 *
 * The row count is 4,000 because that is both docs/03's cap ("Result rows (<= 4,000)") and the
 * number Prompt 8's second acceptance criterion measures a scroll against.
 */
const ROW_COUNT = 4000;

const SENTINELS = {
  awayFromHigh: 100,
  positiveDays: 0,
  circuits: 250,
} as const;

function Section({
  id,
  title,
  description,
  children,
}: {
  id: string;
  title: string;
  description: string;
  children: React.ReactNode;
}) {
  return (
    <section aria-labelledby={id} className="space-y-3">
      <div className="space-y-1">
        <h2 id={id} className="text-base font-semibold tracking-tight">
          {title}
        </h2>
        <p className="max-w-prose text-sm text-muted-foreground">{description}</p>
      </div>
      {children}
    </section>
  );
}

export function KitchenSink() {
  const [density, setDensity] = useState<Density>("comfortable");
  const [repeatHeader, setRepeatHeader] = useState(true);
  const [loading, setLoading] = useState(false);
  const [factor, setFactor] = useState<string>("avg_sharpe_12_6_3_1");
  const [awayFromHigh, setAwayFromHigh] = useState<number>(SENTINELS.awayFromHigh);
  const [positiveDays, setPositiveDays] = useState<number>(SENTINELS.positiveDays);
  const [circuits, setCircuits] = useState<number>(999);

  const rows = useMemo(() => makeRows(ROW_COUNT), []);
  const series = useMemo(() => makeSeries(30, 7), []);
  const fallingSeries = useMemo(() => makeSeries(30, 99).map((value) => 200 - value), []);

  const columns = useMemo<Array<ColumnDef<DemoRow, unknown>>>(
    () => [
      { id: "rank", header: "#", accessorKey: "rank", size: 56,
        cell: (info) => <span className="tnum text-muted-foreground">{String(info.getValue())}</span> },
      { id: "symbol", header: "Symbol", accessorKey: "symbol", size: 110,
        cell: (info) => <span className="font-medium">{String(info.getValue())}</span> },
      { id: "name", header: "Name", accessorKey: "name", size: 220 },
      { id: "sortingFactor", header: "Sorting Factor", accessorKey: "sortingFactor", size: 130,
        cell: (info) => <span className="w-full text-right tnum">{formatNumber(info.getValue() as number, { decimals: 2 })}</span> },
      { id: "closeRaw", header: "Last Close", accessorKey: "closeRaw", size: 110,
        cell: (info) => <span className="w-full text-right tnum">{formatNumber(info.getValue() as number, { decimals: 2 })}</span> },
      { id: "series", header: "Series", accessorKey: "series", size: 80 },
      { id: "marketcapCr", header: "Marketcap", accessorKey: "marketcapCr", size: 130,
        cell: (info) => <span className="w-full text-right tnum">{formatCrore(info.getValue() as number)}</span> },
      { id: "ret12m", header: "1Yr Return", accessorKey: "ret12m", size: 110,
        cell: (info) => {
          const value = info.getValue() as number;
          return (
            <span className={cn("w-full text-right tnum", value >= 0 ? "text-positive" : "text-negative")}>
              {formatPercent(value)}
            </span>
          );
        } },
      { id: "sharpe12m", header: "1Yr Sharpe", accessorKey: "sharpe12m", size: 110,
        cell: (info) => <span className="w-full text-right tnum">{formatNumber(info.getValue() as number, { decimals: 2 })}</span> },
      { id: "vol12m", header: "1Yr Volatility", accessorKey: "vol12m", size: 120,
        cell: (info) => <span className="w-full text-right tnum">{formatFraction(info.getValue() as number)}</span> },
      { id: "beta12m", header: "Beta", accessorKey: "beta12m", size: 90,
        cell: (info) => <span className="w-full text-right tnum">{formatNumber(info.getValue() as number, { decimals: 2 })}</span> },
      { id: "ma200", header: "MA 200", accessorKey: "ma200", size: 110,
        cell: (info) => <span className="w-full text-right tnum">{formatNumber(info.getValue() as number, { decimals: 2 })}</span> },
    ],
    [],
  );

  return (
    <div className="space-y-12">
      <header className="space-y-2">
        <h1 className="text-2xl font-semibold tracking-tight">Kitchen sink</h1>
        <p className="max-w-prose text-sm text-muted-foreground">
          Every core primitive, rendered in the real shell. Switch the theme in the top bar to check
          both; every colour that carries text here is verified at 4.5:1 or better in each.
        </p>
      </header>

      <Section
        id="stat-cards"
        title="StatCard, Sparkline and MetricGrid"
        description="Value, direction and the instrument's own median. The arrow and the sign carry the meaning as well as the colour, so the cards are readable in greyscale."
      >
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <StatCard
            label="1-year return"
            value={formatPercent(753)}
            changeValue={753}
            changeLabel="vs. index"
            median="41.20%"
            sparkline={<Sparkline values={series} label="30-day close" />}
          />
          <StatCard
            label="1-year volatility"
            value={formatFraction(0.5793)}
            changeValue={-4.2}
            changeLabel="vs. median"
            median="48.10%"
            sparkline={<Sparkline values={fallingSeries} label="30-day volatility" />}
          />
          <StatCard label="Beta" value={formatNumber(0.85, { decimals: 2 })} median="1.02" />
          <StatCard label="Marketcap" value={formatCrore(38264)} loading={loading} median="—" />
        </div>

        <div className="rounded-md border border-border bg-card p-4">
          <MetricGrid
            caption="Factor values by window, with each value's percentile in the current universe"
            columns={["1Y", "9M", "6M", "3M", "1M"]}
            loading={loading}
            rows={[
              { family: "Absolute return", cells: [
                { display: "753.00%", percentile: 0.99 },
                { display: "412.10%", percentile: 0.96 },
                { display: "188.40%", percentile: 0.91 },
                { display: "64.20%", percentile: 0.78 },
                { display: "8.10%", percentile: 0.55 },
              ] },
              { family: "Sharpe return", cells: [
                { display: "13.00", percentile: 0.98 },
                { display: "9.40", percentile: 0.94 },
                { display: "4.40", percentile: 0.83 },
                { display: "2.92", percentile: 0.71 },
                { display: "0.67", percentile: 0.48 },
              ] },
              { family: "RSI", cells: [
                { display: "71.4020", percentile: 0.88 },
                { display: "68.1140", percentile: 0.81 },
                { display: "63.9902", percentile: 0.72 },
                { display: "58.2210", percentile: 0.6 },
                { display: "51.0034", percentile: 0.44 },
              ] },
            ]}
          />
        </div>
      </Section>

      <Section
        id="filters"
        title="FilterAccordion, SentinelNumberInput and FactorCombobox"
        description="A collapsed group shows how many of its filters are doing something. A field at its sentinel renders off and says so in its description, which is what the input points at with aria-describedby."
      >
        <div className="grid gap-6 lg:grid-cols-2">
          <div className="rounded-md border border-border bg-card p-4">
            <div className="space-y-1.5 pb-4">
              <Label id="sort-by-label" htmlFor="sort-by-label">
                Sort By (Factor)
              </Label>
              <FactorCombobox
                factors={DEMO_FACTORS}
                value={factor}
                onChange={setFactor}
                labelledBy="sort-by-label"
              />
            </div>
            <FilterAccordion
              defaultOpen={["away-from-high"]}
              sections={[
                {
                  id: "away-from-high",
                  title: "Away from High Filters",
                  activeCount: awayFromHigh === SENTINELS.awayFromHigh ? 0 : 1,
                  content: (
                    <SentinelNumberInput
                      label="Within % of all-time high"
                      value={awayFromHigh}
                      onChange={setAwayFromHigh}
                      sentinel={SENTINELS.awayFromHigh}
                      sentinelHint="Keep value as 100 if you want to ignore this filter."
                      min={0}
                      max={100}
                      suffix="%"
                    />
                  ),
                },
                {
                  id: "positive-days",
                  title: "Percentage of Positive Days",
                  activeCount: positiveDays === SENTINELS.positiveDays ? 0 : 1,
                  content: (
                    <SentinelNumberInput
                      label="Minimum positive days, 1 year"
                      value={positiveDays}
                      onChange={setPositiveDays}
                      sentinel={SENTINELS.positiveDays}
                      sentinelHint="Keep value as 0 if you want to ignore this filter."
                      min={0}
                      max={100}
                      suffix="%"
                    />
                  ),
                },
                {
                  id: "circuits",
                  title: "Circuit Filters",
                  activeCount: circuits > SENTINELS.circuits ? 0 : 1,
                  content: (
                    <SentinelNumberInput
                      label="Maximum circuit days, 1 year"
                      value={circuits}
                      onChange={setCircuits}
                      sentinel={SENTINELS.circuits}
                      mode="above"
                      sentinelHint="Any value above 250 ignores this filter — a year has fewer trading days than that."
                      min={0}
                    />
                  ),
                },
              ]}
            />
          </div>

          <div className="space-y-4">
            <EmptyState
              title="0 results"
              reason="The 1-year filters exclude instruments listed after 19 Aug 2025, and every remaining name fails the ₹1 crore median-volume floor."
              action={{ label: "Loosen the median-volume filter", onClick: () => undefined }}
            />
            <ErrorState
              error={{
                type: "payment-required",
                title: "Your plan does not include this feature",
                status: 402,
                detail: "'export_csv' is not included in your plan.",
                instance: "/api/v1/screens/exmpl0000001/csv",
                upgrade_url: "/pricing",
              }}
              onRetry={() => undefined}
            />
            <div className="flex flex-wrap gap-2">
              <Badge>Neutral</Badge>
              <Badge variant="accent">Accent</Badge>
              <Badge variant="positive">▲ Positive</Badge>
              <Badge variant="negative">▼ Negative</Badge>
              <Badge variant="warning">Degraded</Badge>
              <Badge variant="outline">Outline</Badge>
            </div>
            <div className="flex flex-wrap gap-2">
              <Button variant="primary">Update &amp; Apply Filters</Button>
              <Button variant="secondary">Secondary</Button>
              <Button variant="outline">Outline</Button>
              <Button variant="ghost">Ghost</Button>
              <Button variant="destructive">Delete</Button>
              <Button variant="link">Link</Button>
            </div>
          </div>
        </div>
      </Section>

      <Section
        id="data-table"
        title={`DataTable — ${ROW_COUNT.toLocaleString("en-IN")} rows, virtualised`}
        description="Sticky header, an optional header repeat every 16 rows, and a density toggle. Click into the grid and use the arrow keys: it is one tab stop with cell-by-cell movement inside."
      >
        <div className="flex flex-wrap items-center gap-6">
          <div className="flex items-center gap-2">
            <Switch
              id="density"
              checked={density === "compact"}
              onCheckedChange={(checked) => setDensity(checked ? "compact" : "comfortable")}
            />
            <Label htmlFor="density">Compact density</Label>
          </div>
          <div className="flex items-center gap-2">
            <Switch id="repeat-header" checked={repeatHeader} onCheckedChange={setRepeatHeader} />
            <Label htmlFor="repeat-header">
              Repeat header every {DEFAULT_REPEAT_HEADER_EVERY} rows
            </Label>
          </div>
          <div className="flex items-center gap-2">
            <Switch id="loading" checked={loading} onCheckedChange={setLoading} />
            <Label htmlFor="loading">Loading state</Label>
          </div>
        </div>

        <DataTable
          data={rows}
          columns={columns}
          label={`Demo screen results, ${ROW_COUNT} rows`}
          density={density}
          repeatHeaderEvery={repeatHeader ? DEFAULT_REPEAT_HEADER_EVERY : 0}
          loading={loading}
          height={520}
        />
        <p className="text-xs text-muted-foreground">
          Sorting a column re-orders these rows in the browser. It does not re-run the screen, so
          the ranks stay as the server computed them.
        </p>
      </Section>

      <Section
        id="disclaimer"
        title="Disclaimer"
        description="Rendered by the shell on every analytics surface, so a new page cannot ship without it."
      >
        <Disclaimer variant="block" />
      </Section>
    </div>
  );
}
