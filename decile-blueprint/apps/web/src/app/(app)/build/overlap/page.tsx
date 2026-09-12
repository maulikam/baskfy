import type { Metadata } from "next";

import { OverlapPanel } from "@/components/overlap/overlap-panel";
import { ScreenPicker } from "@/components/overlap/screen-picker";
import { Answer, Mark } from "@/components/shell/answer";
import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { fetchOverlapSources } from "@/lib/overlap/fetch-overlap";
import { intersectionOf } from "@/lib/overlap/overlap";
import { formatTradeDate } from "@/lib/format";
import { PAGES } from "@/lib/vocabulary";

/**
 * `/build/overlap` — names that land on more than one Build scan.
 *
 * Two lists on one page, in the order a person asks:
 *
 *   1. Volume breakout ∩ Three weeks tight
 *   2. Screens ∩ Volume breakout ∩ Three weeks tight
 *
 * Read-only. Nothing here queues a scan or places an order — it only intersects what those
 * surfaces already published (plus one fresh screen run so the three-way list is current).
 */

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/build/overlap"].title,
  description: PAGES["/build/overlap"].blurb,
  robots: { index: false, follow: false },
};

function firstParam(
  value: string | string[] | undefined,
): string | undefined {
  if (Array.isArray(value)) return value[0];
  return value;
}

export default async function BuildOverlapPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const sources = await fetchOverlapSources(firstParam(params.screen) ?? "");

  const pair = intersectionOf([sources.vbt, sources.twt]);
  const triple = intersectionOf([sources.screen, sources.vbt, sources.twt]);

  const asOfDates = [sources.asOf.vbt, sources.asOf.twt, sources.asOf.screen].filter(
    (value): value is string => Boolean(value),
  );
  const asOfLabel =
    asOfDates.length === 0
      ? null
      : [...new Set(asOfDates)].map((date) => formatTradeDate(date)).join(" · ");

  return (
    <div className="flex w-full max-w-[104rem] flex-col gap-6">
      <PageHeader
        title={PAGES["/build/overlap"].title}
        blurb={PAGES["/build/overlap"].blurb}
        meta={
          <span className="flex flex-wrap items-center gap-x-4 gap-y-2">
            {asOfLabel ? <span>As of {asOfLabel}</span> : null}
            <ScreenPicker screens={sources.screens} selected={sources.screen.key} />
          </span>
        }
      />
      <SectionTabs section="build" />

      <Answer
        footnote="Symbols only — check each name on Volume breakout, Three weeks tight, or the screen itself before acting."
      >
        {!pair.available ? (
          <>Overlap cannot be computed until Volume breakout and Three weeks tight have been read.</>
        ) : (
          <>
            <Mark>{pair.sharedCount}</Mark>{" "}
            {pair.sharedCount === 1 ? "name sits" : "names sit"} on both Volume breakout and
            Three weeks tight
            {triple.available ? (
              triple.sharedCount > 0 ? (
                <>
                  ; <Mark>{triple.sharedCount}</Mark> also clear the selected screen
                </>
              ) : (
                <>; none of them also clear the selected screen</>
              )
            ) : null}
            .
          </>
        )}
      </Answer>

      <OverlapPanel
        testId="overlap-pair"
        title="Volume breakout ∩ Three weeks tight"
        blurb="Names that printed a volume-breakout signal and are also quiet for three weeks."
        result={pair}
      />

      <OverlapPanel
        testId="overlap-triple"
        title="Screens ∩ Volume breakout ∩ Three weeks tight"
        blurb={`Names on “${sources.screen.label}”, Volume breakout, and Three weeks tight together.`}
        result={triple}
      />
    </div>
  );
}
