import type { Metadata } from "next";

import { BreadthHistory, type BreadthKey } from "@/components/market/breadth-history";
import { BreadthGauge } from "@/components/market/breadth-gauge";
import { MarketHealthControls } from "@/components/market/market-health-controls";
import { formatTradeDate } from "@/lib/format";
import { DEFAULT_RANGE, rangeDays } from "@/lib/market/ranges";
import { HEALTH_UNIVERSES } from "@/lib/market/universes";
import { fetchMarketHealth, fetchMarketHealthHistory } from "@/lib/market/fetch";

/**
 * `/market-health` — docs/01 §6, docs/08 §"Market Health". docs/08 §Routes: "RSC + client
 * universe switcher".
 *
 * Both the universe and the range live in the URL, so the server fetches exactly what is drawn
 * and the view is shareable. The gauges and the four history charts come from two requests.
 */
export const revalidate = 3600;

export const metadata: Metadata = {
  title: "Market Health",
  description:
    "Breadth across the NSE universes: share of stocks above their 200- and 50-day averages, " +
    "near their all-time high, and positive over one year.",
};

/** docs/01 §6's four gauges, in the order and wording the reference product renders them. */
const SERIES: readonly { key: BreadthKey; label: string }[] = [
  { key: "pct_above_200dma", label: "Above 200 DMA" },
  { key: "pct_above_50dma", label: "Above 50 DMA" },
  { key: "pct_within_10pct_ath", label: "Within 10% of ATH" },
  { key: "pct_ret_1y_positive", label: "1Y Return > 0%" },
];

function toNumber(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined || value === "") return null;
  const numeric = typeof value === "string" ? Number(value) : value;
  return Number.isNaN(numeric) ? null : numeric;
}

/** `2026-08-18` minus N days, as an ISO date. Pure: the anchor is the API's date, not the clock. */
function daysBefore(iso: string, days: number): string {
  const anchor = new Date(`${iso}T00:00:00Z`);
  anchor.setUTCDate(anchor.getUTCDate() - days);
  return anchor.toISOString().slice(0, 10);
}

function isKnownUniverse(slug: string | undefined): slug is string {
  return slug !== undefined && HEALTH_UNIVERSES.some((option) => option.slug === slug);
}

export default async function MarketHealthPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const requested = typeof params.universe === "string" ? params.universe : undefined;
  const universe = isKnownUniverse(requested) ? requested : "nifty-500";
  const range = typeof params.range === "string" ? params.range : DEFAULT_RANGE;

  /*
   * Sequential, not parallel, and deliberately so: the range is measured back from the date the
   * API is actually serving, not from wall-clock time. Anchoring it to `Date.now()` would slide
   * the window away from the published data every day the pipeline did not run — and would make
   * this component's output depend on when it rendered, which is also what React's purity rule
   * objects to.
   */
  const health = await fetchMarketHealth(universe);
  const history = await fetchMarketHealthHistory(universe, daysBefore(health.as_of, rangeDays(range)));

  return (
    <div className="flex flex-col gap-5">
      <header className="flex flex-col gap-1">
        <h1 className="text-xl font-semibold tracking-tight">
          Market Health — {health.universe.name}
        </h1>
        <p className="text-sm text-muted-foreground">
          {/* docs/01 §6's "Data available from 1st Nov 2024" — read from the earliest stored row
              rather than written as a constant, so it stops lying the day the backfill goes back
              further. */}
          {health.data_available_from
            ? `Data available from ${formatTradeDate(health.data_available_from)}.`
            : "No breadth has been recorded for this universe yet."}{" "}
          {health.constituent_count === null || health.constituent_count === undefined
            ? null
            : `${health.constituent_count} constituents on ${formatTradeDate(health.as_of)}.`}
        </p>
      </header>

      <MarketHealthControls
        universes={HEALTH_UNIVERSES}
        universe={universe}
        range={range}
      />

      <section aria-label="Breadth gauges" className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {health.gauges.map((gauge) => (
          <BreadthGauge key={gauge.key} label={gauge.label} value={toNumber(gauge.value)} />
        ))}
      </section>

      <section aria-labelledby="history-heading" className="flex flex-col gap-3">
        <div>
          <h2 id="history-heading" className="text-sm font-semibold">
            History
          </h2>
          <p className="text-xs text-muted-foreground">
            Each breadth series over the selected range, with {health.universe.name}&rsquo;s own
            index level overlaid. This is the analytical use of breadth data, and it is what the
            reference product does not show.
          </p>
        </div>
        <div className="grid gap-3 xl:grid-cols-2">
          {SERIES.map((series) => (
            <BreadthHistory
              key={series.key}
              points={history.points}
              series={series}
              universeName={health.universe.name}
            />
          ))}
        </div>
      </section>
    </div>
  );
}
