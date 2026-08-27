"use client";

import Link from "next/link";
import { useState } from "react";

import { AmountsProvider } from "@/components/portfolio/amounts";
import { AttentionRibbon } from "@/components/portfolio/attention-ribbon";
import { CombinedChart } from "@/components/portfolio/combined-chart";
import { HeroMetrics } from "@/components/portfolio/hero-metrics";
import { InspectorDrawer } from "@/components/portfolio/inspector-drawer";
import { OverviewEmpty, isEmptyAccount } from "@/components/portfolio/overview-empty";
import { OverviewHeader } from "@/components/portfolio/overview-header";
import { PortfolioTable } from "@/components/portfolio/portfolio-table";
import { loadOverviewChart, type InspectorData } from "@/lib/portfolio/inspector";
import { rowsForTab, type NavRange, type NavSeries, type Overview, type PortfolioRow } from "@/lib/portfolio/overview";

/**
 * §6's single screen, top to bottom, assembled.
 *
 * Header (§6.1) · hero metrics (§6.2) · combined chart (§6.3) · needs-attention ribbon (§6.4) ·
 * portfolio table with its inspector drawer (§6.5). §6.6's Unallocated centrepiece and §6.7's
 * new-portfolio flow are a sibling's work and are composed in by the page, not by this component.
 *
 * Three pieces of state live here and nowhere lower down, because they are shared:
 *
 * * **The selected row.** The table raises it, the drawer consumes it, and neither owns it — a
 *   drawer that fetched its own selection would be a second source of truth for what is open.
 * * **The rows currently on screen.** §6.1's Export writes what the reader is looking at, not the
 *   whole account, so the grouping tab's choice has to reach the header.
 * * **The chart's series.** §6.3's range pills refetch it and nothing else (see `loadOverviewChart`).
 *
 * `AmountsProvider` wraps everything so §6.1's show/hide toggle reaches every rupee on the page,
 * including the ones inside the drawer.
 */

export interface PortfolioOverviewScreenProps {
  overview: Overview;
  /** §6.6/§6.7 content, composed in by the page so this component owns only §6.1–§6.5. */
  children?: React.ReactNode;
  /** Injected in tests. */
  loadChart?: (range: NavRange) => Promise<NavSeries | null>;
  loadInspectorData?: (portfolioId: number) => Promise<InspectorData>;
}

export function PortfolioOverviewScreen({
  overview,
  children,
  loadChart = loadOverviewChart,
  loadInspectorData,
}: PortfolioOverviewScreenProps) {
  const [selected, setSelected] = useState<PortfolioRow | null>(null);
  const [visibleRows, setVisibleRows] = useState<readonly PortfolioRow[]>(() =>
    rowsForTab(overview, "ALL"),
  );
  const [chart, setChart] = useState<NavSeries>(overview.chart);
  const [chartLoading, setChartLoading] = useState(false);
  const [chartFailed, setChartFailed] = useState(false);
  const empty = isEmptyAccount(overview);

  function changeRange(range: NavRange) {
    setChartLoading(true);
    setChartFailed(false);
    void loadChart(range)
      .then((next) => {
        if (next) setChart(next);
        else setChartFailed(true);
      })
      .finally(() => setChartLoading(false));
  }

  return (
    <AmountsProvider>
      <div className="flex flex-col gap-6">
        <OverviewHeader overview={overview} rows={visibleRows} />

        {empty ? (
          /* Nothing is connected and nothing is held. Five hero metrics all reading "—", an
             empty chart and an empty table would be five ways of saying the same thing and no
             way of fixing it, so the screen is the one action instead (§11 criterion 8). */
          <OverviewEmpty />
        ) : (
          <>
            <HeroMetrics hero={overview.hero} />

            <CombinedChart chart={chart} onRangeChange={changeRange} loading={chartLoading} />
            {chartFailed ? (
              <p role="alert" className="text-xs text-negative">
                That range could not be loaded. The chart above is still the range it was.
              </p>
            ) : null}

            <AttentionRibbon items={overview.attention ?? []} />

            {children}

            <PortfolioTable
              overview={overview}
              onSelect={setSelected}
              onRowsChange={setVisibleRows}
            />

            <InspectorDrawer
              row={selected}
              onClose={() => setSelected(null)}
              {...(loadInspectorData ? { load: loadInspectorData } : {})}
            />
          </>
        )}

        <p className="text-xs text-muted-foreground">
          Read-only. Every action here ends in an order plan you take to your broker; this page
          never places an order.{" "}
          <Link href="/disclaimer" className="underline-offset-4 hover:underline">
            Disclaimer
          </Link>
        </p>
      </div>
    </AmountsProvider>
  );
}
