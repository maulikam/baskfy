import type { Metadata } from "next";

import type { BreadthKey } from "@/components/market/breadth-history";
// The chart itself is loaded on demand — see `breadth-history-lazy.tsx`.
import { BreadthHistory } from "@/components/market/breadth-history-lazy";
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
  const from = daysBefore(health.as_of, rangeDays(range));
  const history = await fetchMarketHealthHistory(universe, from);

  // True when the reader asked for more history than exists. `data_available_from` is the
  // earliest stored breadth row, which is what the chart can actually start at.
  const truncated =
    health.data_available_from !== null &&
    health.data_available_from !== undefined &&
    from < health.data_available_from;

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

        {/*
          The range selector offers 5Y whether or not five years exist. Saying "Data available
          from ..." in the header and then drawing a shorter line than the button implies is how
          a reader concludes the filter is broken -- so when the requested window starts before
          the data does, the page says so where the chart is.
        */}
        {truncated && (
          <p role="status" className="rounded-md border bg-muted/40 p-2.5 text-xs">
            The selected range starts before the data does. These charts begin{" "}
            <strong>{formatTradeDate(health.data_available_from ?? health.as_of)}</strong>, which is
            as far back as breadth has been computed &mdash; not as far back as prices go.
          </p>
        )}

        {/*
          Not a footnote. Breadth is computed over the constituents on each date, and for every
          date before the membership was published those constituents are today's, carried
          backwards and stored as `source = 'derived'`. That biases the past upward, and a reader
          comparing 2025's breadth with today's deserves to know before they draw a conclusion
          rather than after.
        */}
        <p
          role="status"
          className="rounded-md border border-amber-500/40 bg-amber-500/10 p-2.5 text-xs"
        >
          <strong>Historical breadth is survivorship-biased.</strong> NSE publishes index
          constituents for today only, so for dates before that the current members are carried
          backwards. A company dropped from {health.universe.name} after falling is missing from
          its own history, which makes the past look healthier than it was. Today&rsquo;s gauges
          are unaffected; the stored rows are marked so backtests can exclude them.
        </p>
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
