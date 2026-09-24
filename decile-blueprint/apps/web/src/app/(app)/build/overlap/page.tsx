import type { Metadata } from "next";

import { CandidatesTable } from "@/components/overlap/candidates-table";
import { OverlapMatrix } from "@/components/overlap/overlap-matrix";
import { OverlapPanel } from "@/components/overlap/overlap-panel";
import { ScreenPicker } from "@/components/overlap/screen-picker";
import { Answer, Mark } from "@/components/shell/answer";
import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import type { OverlapScope } from "@/lib/overlap/candidates";
import { fetchCandidates } from "@/lib/overlap/fetch-candidates";
import { fetchOverlapSources } from "@/lib/overlap/fetch-overlap";
import { intersectionOf, membershipOf } from "@/lib/overlap/overlap";
import { formatTradeDate } from "@/lib/format";
import { PAGES } from "@/lib/vocabulary";

import { correctTag, filingsScanStatus, startFilingsScan } from "./actions";

/**
 * `/build/overlap` — names that land on more than one Build scan.
 *
 * The table lists every name across Volume breakout, Three weeks tight, Swing, and up to three
 * screens. The named lists below it are the pairwise (and one three-way) cuts a person still
 * asks for by name.
 *
 * Nothing here places an order. It intersects what those surfaces already published (plus a
 * fresh run of each picked screen so those columns are current). Its writes are a person's
 * correction of a tag, and the "Scan filings with Laya" button, which reads the listed names'
 * exchange filings into the Results and Filing cells (25 Sep 2026).
 *
 * **Today's candidates (`GET /overlap`) sits above the matrix.** The matrix was symbols only —
 * "check each name on Volume breakout, Swing, Three weeks tight, or the screen itself before
 * acting" — and the table is that check, served from the same stored rows: each strategy's own
 * word on the name, whether it could act, the exchange filing and result date the swing feed
 * linked, and the live mark. It adds no number the strategies did not write; `?scope=all` widens
 * it from rows a strategy could act on to every row the scans wrote.
 */

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/build/overlap"].title,
  description: PAGES["/build/overlap"].blurb,
  robots: { index: false, follow: false },
};

function scopeParam(value: string | string[] | undefined): OverlapScope {
  return value === "all" ? "all" : "actionable";
}

function screenParams(value: string | string[] | undefined): string[] {
  if (value === undefined) return [];
  return (Array.isArray(value) ? value : [value])
    .map((entry) => entry.trim())
    .filter(Boolean);
}

export default async function BuildOverlapPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const scope = scopeParam(params.scope);
  const [sources, candidates] = await Promise.all([
    fetchOverlapSources(screenParams(params.screen)),
    fetchCandidates(scope),
  ]);
  const selectedScreens = sources.selectedScreens;
  const primaryScreen = selectedScreens[0];

  const membership = membershipOf([
    sources.vbt,
    sources.twt,
    sources.swing,
    ...selectedScreens,
  ]);
  const pair = intersectionOf([sources.vbt, sources.twt]);
  const swingTwt = intersectionOf([sources.swing, sources.twt]);
  const swingVbt = intersectionOf([sources.swing, sources.vbt]);
  const triple = primaryScreen
    ? intersectionOf([primaryScreen, sources.vbt, sources.twt])
    : intersectionOf([sources.vbt, sources.twt]);

  const asOfDates = [
    sources.asOf.vbt,
    sources.asOf.twt,
    sources.asOf.swing,
    ...sources.asOf.screens,
  ].filter((value): value is string => Boolean(value));
  const asOfLabel =
    asOfDates.length === 0
      ? null
      : [...new Set(asOfDates)].map((date) => formatTradeDate(date)).join(" · ");

  const sharedAcross = membership.rows.filter((row) => row.count >= 2).length;
  const screenNames = selectedScreens.map((screen) => screen.label);
  const screenList =
    screenNames.length === 0
      ? "a screen"
      : screenNames.length === 1
        ? `“${screenNames[0]}”`
        : screenNames.length === 2
          ? `“${screenNames[0]}” and “${screenNames[1]}”`
          : `“${screenNames.slice(0, -1).join("”, “")}”, and “${screenNames[screenNames.length - 1]}”`;

  return (
    <div className="flex w-full max-w-[104rem] flex-col gap-6">
      <PageHeader
        title={PAGES["/build/overlap"].title}
        blurb={PAGES["/build/overlap"].blurb}
        meta={
          <span className="flex flex-wrap items-center gap-x-4 gap-y-2">
            {asOfLabel ? <span>As of {asOfLabel}</span> : null}
            <ScreenPicker
              screens={sources.screens}
              selected={selectedScreens.map((screen) => screen.key)}
            />
          </span>
        }
      />
      <SectionTabs section="build" />

      <Answer
        footnote="Each strategy's own word on a name is restated below, not re-scored — confirm it on that strategy's page before acting."
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
            Three weeks tight, Swing, and {screenList}
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

      <CandidatesTable
        candidates={candidates}
        scope={scope}
        correctTag={correctTag}
        scanFilings={{ start: startFilingsScan, status: filingsScanStatus }}
      />

      <OverlapMatrix
        membership={membership}
        screenKeys={selectedScreens.map((screen) => screen.key)}
      />

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
        blurb={
          primaryScreen
            ? `Names on “${primaryScreen.label}”, Volume breakout, and Three weeks tight together.`
            : "Names on a screen, Volume breakout, and Three weeks tight together."
        }
        result={triple}
      />
    </div>
  );
}
