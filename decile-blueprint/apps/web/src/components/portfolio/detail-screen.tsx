"use client";

import { Eye, EyeOff } from "lucide-react";
import { useState, type ReactNode } from "react";

import { AmountsProvider, useAmounts } from "@/components/portfolio/amounts";
import { CombinedChart } from "@/components/portfolio/combined-chart";
import { DetailActivity } from "@/components/portfolio/detail-activity";
import { DetailHoldings } from "@/components/portfolio/detail-holdings";
import { DetailSourcePanel } from "@/components/portfolio/detail-source-panel";
import { DetailSummary } from "@/components/portfolio/detail-summary";
import { SourceBadge } from "@/components/portfolio/source-badge";
import { PageHeader } from "@/components/shell/page-header";
import { Button } from "@/components/ui/button";
import { accessToken } from "@/lib/api/browser";
import { apiOrigin } from "@/lib/api/config";
import { isBasketBacked, type DetailHoldingRow } from "@/lib/portfolio/detail-view";
import type {
  ActivityItem,
  NavRange,
  NavSeries,
  PortfolioDetail,
} from "@/lib/portfolio/overview";

/**
 * `PORTFOLIO_REDESIGN.md` §7's detail page, "v1 minimal", assembled in the spec's own order.
 *
 * Summary (value, invested, both P&Ls, the labelled headline metric, the benchmark difference,
 * cash, last sync) · Performance (the EOD NAV chart with its benchmark overlay and drawdown) ·
 * Holdings · Activity · Source panel. §7 names heatmaps and rolling returns as LATER and they are
 * not here; a v1 that quietly grew the two hardest panels is a v1 nobody can ship.
 *
 * ## Why this is one page and not the drawer again
 *
 * §6.5's inspector drawer answers *"what is this row?"* without losing the reader's place in the
 * table, and it puts performance, holdings and activity behind three tabs to fit 40% of a screen.
 * §7 is the opposite trade: the reader has already chosen this portfolio, has the whole width,
 * and the question is *"why does it look like that"* — which is answered by having the chart, the
 * positions and the sequence of events visible at once rather than one at a time. Same payloads,
 * different arrangement, and the drawer keeps its link here for the reader who wants it.
 *
 * `AmountsProvider` wraps the page so §6.1's show/hide-amounts courtesy reaches every rupee on
 * it — the summary, the table, the activity feed and the chart's own axis.
 *
 * The chart's range pills refetch only the NAV series. Re-rendering the page for a range would
 * throw away the reader's scroll position for a change that affects one panel.
 */

export interface PortfolioDetailScreenProps {
  detail: PortfolioDetail;
  nav: NavSeries | null;
  activity: readonly ActivityItem[] | null;
  failures?: {
    readonly nav?: string | null;
    readonly activity?: string | null;
  };
  /** Composed in by the page — the manage actions the older investment ledger still owns. */
  children?: ReactNode;
  /** Injected in tests; the default reaches the API from the browser. */
  loadRange?: (portfolioId: number, range: NavRange) => Promise<NavSeries | null>;
}

/** §6.3's range pills against `GET /portfolio/{id}/nav`, without a page navigation. */
async function loadNavRange(portfolioId: number, range: NavRange): Promise<NavSeries | null> {
  try {
    const token = await accessToken();
    const response = await fetch(
      `${apiOrigin()}/api/v1/portfolio/${encodeURIComponent(String(portfolioId))}/nav?range=${encodeURIComponent(range)}`,
      { headers: token ? { Authorization: `Bearer ${token}` } : {} },
    );
    if (!response.ok) return null;
    return (await response.json()) as NavSeries;
  } catch {
    return null;
  }
}

export function PortfolioDetailScreen({
  detail,
  nav,
  activity,
  failures,
  children,
  loadRange = loadNavRange,
}: PortfolioDetailScreenProps) {
  const [chart, setChart] = useState<NavSeries | null>(nav);
  const [chartLoading, setChartLoading] = useState(false);
  const [chartFailed, setChartFailed] = useState(false);

  const summary = detail.summary;
  const panel = detail.source_panel;
  const holdings = (detail.holdings ?? []) as readonly DetailHoldingRow[];

  function changeRange(range: NavRange) {
    setChartLoading(true);
    setChartFailed(false);
    void loadRange(summary.portfolio_id, range)
      .then((next) => {
        if (next) setChart(next);
        else setChartFailed(true);
      })
      .finally(() => setChartLoading(false));
  }

  return (
    <AmountsProvider>
      <div className="flex flex-col gap-6">
        <PageHeader
          title={summary.name}
          blurb="Everything this portfolio is worth, how it got there, and where it came from."
          actions={<AmountsToggle />}
          meta={
            <span className="inline-flex flex-wrap items-center gap-2">
              <SourceBadge row={summary} />
            </span>
          }
        />

        <DetailSummary summary={summary} />

        <section aria-label="Performance" className="flex flex-col gap-2">
          {chart ? (
            <CombinedChart chart={chart} onRangeChange={changeRange} loading={chartLoading} />
          ) : (
            <p
              data-testid="detail-nav-unavailable"
              className="rounded-xl border border-dashed border-border px-4 py-10 text-center text-sm text-muted-foreground"
            >
              {failures?.nav ??
                "No end-of-day valuations have been recorded for this portfolio yet, so there is nothing to plot."}
            </p>
          )}
          {chartFailed ? (
            <p role="alert" className="text-xs text-negative">
              That range could not be loaded. The chart above is still the range it was.
            </p>
          ) : null}
        </section>

        <DetailHoldings
          holdings={holdings}
          basketBacked={isBasketBacked(panel)}
        />

        <DetailActivity items={activity} unavailableReason={failures?.activity ?? null} />

        <DetailSourcePanel panel={panel} summary={summary} />

        {children}
      </div>
    </AmountsProvider>
  );
}

/** §6.1's first control, carried onto §7 so the provider above is not inert. */
function AmountsToggle() {
  const { visible, toggle } = useAmounts();
  return (
    <Button
      variant="secondary"
      size="sm"
      onClick={toggle}
      aria-pressed={!visible}
      data-testid="detail-amounts-toggle"
      className="rounded-full"
    >
      {visible ? <EyeOff aria-hidden="true" /> : <Eye aria-hidden="true" />}
      {visible ? "Hide amounts" : "Show amounts"}
    </Button>
  );
}
