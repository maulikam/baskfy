import type { Metadata } from "next";

import type { BreadthKey } from "@/components/market/breadth-history";
// The chart itself is loaded on demand — see `breadth-history-lazy.tsx`.
import { BreadthHistory } from "@/components/market/breadth-history-lazy";
import { BreadthGauge } from "@/components/market/breadth-gauge";
import { MarketHealthControls } from "@/components/market/market-health-controls";
import { Answer, Mark } from "@/components/shell/answer";
import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { formatTradeDate } from "@/lib/format";
import { PAGES, TERMS, type TermId } from "@/lib/vocabulary";
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
  title: PAGES["/market/mood"].title,
  description:
    "How many stocks are actually rising, not just the index: the share above their one-year and " +
    "three-month trends, near their highest ever, and up over the past twelve months.",
};

/**
 * docs/01 §6's four gauges, in the order the reference product renders them — relabelled by M36.
 *
 * The reference wording ("Above 200 DMA", "Within 10% of ATH") is precise and unreadable to
 * anybody meeting it for the first time, and this is the page a new visitor is most likely to
 * open first. Each entry now points at a `lib/vocabulary` term, which carries the plain label the
 * page renders *and* the professional name, shown on hover. Nothing was dropped; the order of the
 * two was swapped.
 */
const SERIES: readonly { key: BreadthKey; term: TermId }[] = [
  { key: "pct_above_200dma", term: "above_200dma" },
  { key: "pct_above_50dma", term: "above_50dma" },
  { key: "pct_within_10pct_ath", term: "near_high" },
  { key: "pct_ret_1y_positive", term: "positive_1y" },
];

/** The API's gauge key, to the vocabulary entry that explains it. */
const GAUGE_TERMS: Record<string, TermId> = {
  pct_above_200dma: "above_200dma",
  pct_above_50dma: "above_50dma",
  pct_within_10pct_ath: "near_high",
  pct_ret_1y_positive: "positive_1y",
};

/**
 * The number, read back as a sentence.
 *
 * A percentage on a dial tells you what was measured but not what it *means*, and "56.4%" is the
 * kind of figure a reader nods at without absorbing. Saying "about 6 in every 10" is the same
 * fact in the unit people actually think in.
 *
 * It states and never advises: "most companies are in an uptrend" is an observation about stored
 * rows. Anything of the form "so it is a good time to..." would be a recommendation, which
 * docs/11 §Compliance forbids on every surface.
 */
function inTen(value: number | null): string | undefined {
  if (value === null) return undefined;
  const outOfTen = Math.round(value / 10);
  if (outOfTen <= 0) return "Almost none of them.";
  if (outOfTen >= 10) return "Very nearly all of them.";
  return `About ${outOfTen} in every 10.`;
}

/** The same arithmetic as `inTen`, as the phrase the headline highlights. */
function headlineFraction(value: number): string {
  const outOfTen = Math.round(value / 10);
  if (outOfTen <= 0) return "almost none";
  if (outOfTen >= 10) return "very nearly all";
  return `about ${outOfTen} in every 10`;
}

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

  // The 200-day gauge, as a number, or null when the pipeline has not produced one.
  const headline = toNumber(
    health.gauges.find((gauge) => gauge.key === "pct_above_200dma")?.value ?? null,
  );

  return (
    <>
      <SectionTabs section="market" />
      <PageHeader
        title={`${PAGES["/market/mood"].title} — ${health.universe.name}`}
        blurb={PAGES["/market/mood"].blurb}
        meta={
          <>
            {/* docs/01 §6's "Data available from 1st Nov 2024" — read from the earliest stored row
                rather than written as a constant, so it stops lying the day the backfill goes back
                further. */}
            {health.data_available_from
              ? `History goes back to ${formatTradeDate(health.data_available_from)}.`
              : "Nothing has been recorded for this list of stocks yet."}{" "}
            {health.constituent_count === null || health.constituent_count === undefined
              ? null
              : `${health.constituent_count} companies in it on ${formatTradeDate(health.as_of)}.`}
          </>
        }
      />

      {/*
        The finding, before the measurements. The 200-day series is the headline because it is the
        slowest and therefore the one least likely to be describing a single week.
      */}
      {headline === null ? null : (
        <Answer
          footnote={
            <>
              Measured on {formatTradeDate(health.as_of)} across the{" "}
              {health.constituent_count ?? "—"} companies in {health.universe.name}. It describes
              where prices have been, and says nothing about where they go next.
            </>
          }
        >
          <Mark>{headlineFraction(headline)}</Mark> companies in {health.universe.name} are trading
          above their own average price for the past year.
        </Answer>
      )}

      <MarketHealthControls universes={HEALTH_UNIVERSES} universe={universe} range={range} />

      <section
        aria-label="How many stocks are rising"
        className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4"
      >
        {health.gauges.map((gauge) => {
          const term = GAUGE_TERMS[gauge.key];
          const value = toNumber(gauge.value);
          return (
            <BreadthGauge
              key={gauge.key}
              label={term ? TERMS[term].label : gauge.label}
              value={value}
              {...(term ? { term } : {})}
              {...(inTen(value) ? { reading: inTen(value) as string } : {})}
            />
          );
        })}
      </section>

      <section aria-labelledby="history-heading" className="flex flex-col gap-3">
        <div>
          <h2 id="history-heading" className="text-lg">
            The same four, over time
          </h2>
          <p className="max-w-[68ch] text-sm leading-relaxed text-muted-foreground">
            Each line is one of the dials above, drawn back across the range you picked, with{" "}
            {health.universe.name}&rsquo;s own level dashed behind it. The gap between the two is
            the thing worth looking for: an index that climbs while the solid line falls is being
            carried by a handful of large companies.
          </p>
        </div>

        {/*
          The range selector offers 5Y whether or not five years exist. Saying "Data available
          from ..." in the header and then drawing a shorter line than the button implies is how
          a reader concludes the filter is broken -- so when the requested window starts before
          the data does, the page says so where the chart is.
        */}
        {truncated && (
          <p role="status" className="rounded-lg border border-border/70 bg-muted/60 p-3 text-xs leading-relaxed">
            You asked for more history than exists. These charts begin{" "}
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
          className="rounded-lg border border-warning/35 bg-warning-muted p-3 text-xs leading-relaxed"
        >
          <strong>The past here looks better than it was.</strong> NSE publishes index
          constituents for today only, so for dates before that the current members are carried
          backwards. A company dropped from {health.universe.name} after falling is missing from
          its own history, which makes the past look healthier than it was. Today&rsquo;s gauges
          are unaffected; the stored rows are marked so the time machine can leave them out.
          (Its proper name is <em>survivorship bias</em>.)
        </p>
        <div className="grid gap-3 xl:grid-cols-2">
          {SERIES.map((series) => (
            <BreadthHistory
              key={series.key}
              points={history.points}
              series={{ key: series.key, label: TERMS[series.term].label }}
              universeName={health.universe.name}
            />
          ))}
        </div>
      </section>
    </>
  );
}
