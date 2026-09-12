"use client";

import type { Route } from "next";
import Link from "next/link";

import {
  Figure,
  MetricValue,
  Panel,
  ShareBar,
} from "@/components/portfolio/detail/primitives";
import type { AllocationBucket, AllocationView } from "@/lib/portfolio/detail-tabs";

/**
 * The Allocation tab: three real cuts, a concentration score, and four honest absences.
 *
 * By security, by broker and by cash are all computable from the payload and are here. Sector,
 * industry, market capitalisation and geography are not, and neither is overlap with your other
 * portfolios — this page reads one portfolio and never fetches the others. §2.2 names the first
 * four; the fifth is PC3's own finding and is recorded in `docs/pc-findings/pc3.md`.
 *
 * ## Cash sits beside the weights rather than inside them
 *
 * A weight here is a share of what the **shares** are worth. Folding idle rupees into the
 * denominator would shrink every position's weight for a reason that has nothing to do with the
 * positions, and would make two portfolios with identical books incomparable because one of them
 * happens to be holding a dividend. Cash gets its own figure instead, which is the question "how
 * much of this is not invested" answered directly.
 *
 * ## Two numbers for concentration, because one of them is unreadable
 *
 * The Herfindahl index is the score an institution asks for and it means nothing to most
 * readers: 1,700 is a number without a scale. So it is paired with the count of equally weighted
 * holdings that would score the same, which is a sentence anyone can act on — a book of thirty
 * where one name is a third of it behaves like a book of nine.
 */

export interface AllocationTabProps {
  allocation: AllocationView;
}

export function AllocationTab({ allocation }: AllocationTabProps) {
  return (
    <>
      <section
        aria-label="How the money is split"
        data-testid="allocation-totals"
        className="overflow-hidden rounded-xl border border-border bg-card"
      >
        <div className="flex flex-wrap divide-x divide-border">
          <div className="min-w-[11rem] flex-1">
            <Figure metric={allocation.holdingsValue} emphasis="hero" />
          </div>
          <div className="min-w-[11rem] flex-1">
            <Figure metric={allocation.cash} emphasis="hero" />
          </div>
          <div className="min-w-[9rem] flex-1">
            <Figure metric={allocation.deployedShare} kind="percent" />
          </div>
          <div className="min-w-[9rem] flex-1">
            <Figure metric={allocation.cashShare} kind="percent" />
          </div>
        </div>
        <p className="border-t border-border px-4 py-2.5 text-xs leading-snug text-muted-foreground">
          Every weight below is a share of what the shares are worth. Cash is stated beside them
          rather than folded into the denominator, because idle rupees shrinking every position&rsquo;s
          weight would be a change in the arithmetic with nothing to do with the positions. This
          portfolio has no stored target exposure to compare the split against.
        </p>
      </section>

      <Panel
        title="How concentrated it is"
        blurb="Concentration is the one risk figure Baskfy can measure from what it holds, and it is measured here rather than estimated."
        testId="allocation-concentration"
      >
        <div className="flex flex-wrap divide-x divide-border border-b border-border">
          <div className="min-w-[9rem] flex-1">
            <Figure metric={allocation.top1} kind="percent" />
          </div>
          <div className="min-w-[9rem] flex-1">
            <Figure metric={allocation.top3} kind="percent" />
          </div>
          <div className="min-w-[9rem] flex-1">
            <Figure metric={allocation.top5} kind="percent" />
          </div>
          <div className="min-w-[9rem] flex-1">
            <Figure metric={allocation.top10} kind="percent" />
          </div>
        </div>
        <div className="flex flex-wrap divide-x divide-border">
          <div className="min-w-[11rem] flex-1">
            <Figure metric={allocation.herfindahl} kind="count" />
          </div>
          <div className="min-w-[11rem] flex-1">
            <Figure metric={allocation.effectiveHoldings} kind="count" />
          </div>
        </div>
        <p
          data-testid="allocation-verdict"
          className="border-t border-border px-4 py-2.5 text-xs leading-snug text-muted-foreground"
        >
          {allocation.herfindahlVerdict}
        </p>
      </Panel>

      <Panel
        title="By security"
        blurb={`${allocation.bySecurity.length} position${allocation.bySecurity.length === 1 ? "" : "s"}, largest first. Positions with no price are last, and are in no total.`}
        testId="allocation-by-security"
      >
        <BucketList buckets={allocation.bySecurity} />
        {allocation.unpricedCount > 0 ? (
          <p
            data-testid="allocation-unpriced"
            className="border-t border-border px-4 py-2.5 text-xs text-muted-foreground"
          >
            <span className="tabular-nums">{allocation.unpricedCount}</span> of these could not be
            priced. They are excluded from every weight and every total above rather than counted
            as zero: counting them as zero would shrink the denominator, inflate every other
            weight, and still add to 100 percent, which is the failure that looks most like
            success.
          </p>
        ) : null}
      </Panel>

      <Panel
        title="By broker account"
        blurb="Where the shares in this portfolio actually sit. One name held at two brokers is two lines above and two accounts here."
        testId="allocation-by-broker"
      >
        <BucketList buckets={allocation.byBroker} />
      </Panel>

      <Panel
        title="By cash against shares"
        blurb="The only split that is not a weight, because cash is the thing the weights are measured without."
        testId="allocation-by-cash"
      >
        <ul className="divide-y divide-border/60">
          <li className="flex items-center justify-between gap-3 px-4 py-2.5">
            <span className="text-sm font-medium">In shares</span>
            <ShareBar metric={allocation.deployedShare} />
          </li>
          <li className="flex items-center justify-between gap-3 px-4 py-2.5">
            <span className="text-sm font-medium">In cash</span>
            <ShareBar metric={allocation.cashShare} />
          </li>
        </ul>
      </Panel>
    </>
  );
}

function BucketList({ buckets }: { buckets: readonly AllocationBucket[] }) {
  if (buckets.length === 0) {
    return (
      <p className="px-4 py-5 text-sm text-muted-foreground">
        Nothing is filed into this portfolio yet, so there is no split to draw.
      </p>
    );
  }
  return (
    <ul className="divide-y divide-border/60">
      {buckets.map((bucket) => (
        <li
          key={bucket.key}
          data-testid={`allocation-bucket-${bucket.key}`}
          className="flex items-center justify-between gap-3 px-4 py-2.5"
        >
          <span className="min-w-0">
            {bucket.href === undefined ? (
              <span className="block truncate text-sm font-medium">{bucket.label}</span>
            ) : (
              <Link
                href={bucket.href as Route}
                data-testid={`allocation-drill-${bucket.key}`}
                className="block truncate text-sm font-medium underline-offset-4 hover:underline"
              >
                {bucket.label}
              </Link>
            )}
            {bucket.secondary ? (
              <span className="block truncate text-xs text-muted-foreground">
                {bucket.secondary}
              </span>
            ) : null}
          </span>
          <span className="flex shrink-0 items-center gap-4">
            <MetricValue
              metric={bucket.value}
              kind="rupees"
              compact={bucket.value.value === null}
              short="not priced"
              className="text-sm"
            />
            <ShareBar metric={bucket.weight} />
          </span>
        </li>
      ))}
    </ul>
  );
}
