import type { Metadata } from "next";

import { OverlapMatrix } from "@/components/overlap/overlap-matrix";
import { OverlapPanel } from "@/components/overlap/overlap-panel";
import { ScreenPicker } from "@/components/overlap/screen-picker";
import { Answer, Mark } from "@/components/shell/answer";
import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { fetchOverlapSources } from "@/lib/overlap/fetch-overlap";
import { intersectionOf, membershipOf } from "@/lib/overlap/overlap";
import { formatTradeDate } from "@/lib/format";
import { PAGES } from "@/lib/vocabulary";

/**
 * `/build/overlap` — names that land on more than one Build scan.
 *
 * The table lists every name across Volume breakout, Three weeks tight, Swing, and the selected
 * screen. The named lists below it are the pairwise (and one three-way) cuts a person still asks
 * for by name.
 *
 * Read-only. Nothing here queues a scan or places an order — it only intersects what those
 * surfaces already published (plus one fresh screen run so the screen column is current).
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

  const membership = membershipOf([sources.vbt, sources.twt, sources.swing, sources.screen]);
  const pair = intersectionOf([sources.vbt, sources.twt]);
  const swingTwt = intersectionOf([sources.swing, sources.twt]);
  const swingVbt = intersectionOf([sources.swing, sources.vbt]);
  const triple = intersectionOf([sources.screen, sources.vbt, sources.twt]);

  const asOfDates = [
    sources.asOf.vbt,
    sources.asOf.twt,
    sources.asOf.swing,
    sources.asOf.screen,
  ].filter((value): value is string => Boolean(value));
  const asOfLabel =
    asOfDates.length === 0
      ? null
      : [...new Set(asOfDates)].map((date) => formatTradeDate(date)).join(" · ");

  const sharedAcross = membership.rows.filter((row) => row.count >= 2).length;

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
        footnote="Symbols only — check each name on Volume breakout, Swing, Three weeks tight, or the screen itself before acting."
      >
        {!membership.available ? (
          <>
            Overlap cannot be computed until Volume breakout, Swing, Three weeks tight, or a
            screen has been read.
          </>
        ) : (
          <>
            <Mark>{sharedAcross}</Mark>{" "}
            {sharedAcross === 1 ? "name sits" : "names sit"} on at least two of Volume breakout,
            Three weeks tight, Swing, and “{sources.screen.label}”
            {pair.available ? (
              <>
                ; <Mark>{pair.sharedCount}</Mark> on both Volume and Tight
              </>
            ) : null}
            {swingTwt.available ? (
              <>
                ; <Mark>{swingTwt.sharedCount}</Mark> on both Swing and Tight
              </>
            ) : null}
            {swingVbt.available ? (
              <>
                ; <Mark>{swingVbt.sharedCount}</Mark> on both Swing and Volume
              </>
            ) : null}
            .
          </>
        )}
      </Answer>

      <OverlapMatrix membership={membership} screenKey={sources.screen.key} />

      <OverlapPanel
        testId="overlap-pair"
        title="Volume breakout ∩ Three weeks tight"
        blurb="Names that printed a volume-breakout signal and are also quiet for three weeks."
        result={pair}
      />

      <OverlapPanel
        testId="overlap-swing-twt"
        title="Swing ∩ Three weeks tight"
        blurb="Names on the swing scan that are also quiet for three weeks."
        result={swingTwt}
      />

      <OverlapPanel
        testId="overlap-swing-vbt"
        title="Swing ∩ Volume breakout"
        blurb="Names on the swing scan that also printed a volume-breakout signal."
        result={swingVbt}
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
