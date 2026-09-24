import type { Metadata } from "next";

import { FnoRiskCaveat, ScanOnly } from "@/components/options/caveats";
import { ChainPanel } from "@/components/options/chain-panel";
import { HeaderStrip } from "@/components/options/header-strip";
import { PositionsPanel } from "@/components/options/positions";
import { SleeveCard } from "@/components/options/sleeve-card";
import { ToneBadge } from "@/components/options/state-chip";
import { Answer, Mark } from "@/components/shell/answer";
import { PageHeader } from "@/components/shell/page-header";
import { OptionsSubNav, SectionTabs } from "@/components/shell/section-tabs";
import { fetchOptionsChain, fetchOptionsToday } from "@/lib/options/fetch";
import type {
  OptionsScan,
  OptionsToday,
  SleeveCode,
} from "@/lib/options/types";
import {
  SLEEVE_SHORT,
  clockLabel,
  emptyReasonText,
  stateText,
} from "@/lib/options/view";
import { PAGES } from "@/lib/vocabulary";

/**
 * `/options` — the NIFTY options tab of `docs/options/05` §2 (OP5).
 *
 * Maulik opens this on his phone and sees each sleeve's state and candidate, the chain and the
 * calendar — **and can change nothing that moves money**. There is no server action under this
 * page: the tab's two writes (an event day, the settings) live on `/options/calendar` and
 * `/me/options`, and neither can build, confirm or close a plan. The desk console's
 * the desk's options page is the only surface with a Confirm (`05` §3). `__tests__/read-only.test.tsx`
 * is the census.
 *
 * **The clock is on the page** (root `CLAUDE.md`'s clock table, options row): the scans are the
 * collector's latest minute while the session is open (`Live · 13:14`, amber when stale), else the
 * last scanned session's close. With the collector off — the state this ships in — there are no
 * scan rows, and the page says *that*, not "nothing today".
 */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/options"].title,
  description: PAGES["/options"].blurb,
};

function scanOf(today: OptionsToday, sleeve: SleeveCode): OptionsScan | null {
  return today.scans.find((scan) => scan.sleeve === sleeve) ?? null;
}

function summary(today: OptionsToday): string {
  return today.scans
    .map(
      (scan) =>
        `${SLEEVE_SHORT[scan.sleeve]} ${stateText(scan.state).label.toLowerCase()}`,
    )
    .join(" · ");
}

function Group({
  title,
  sleeves,
  today,
}: {
  title: string;
  sleeves: readonly SleeveCode[];
  today: OptionsToday;
}) {
  return (
    <section className="space-y-3" aria-label={title}>
      <h2 className="text-sm font-medium uppercase tracking-wide text-muted-foreground">
        {title}
      </h2>
      <div className="grid gap-3 lg:grid-cols-2">
        {sleeves.map((sleeve) => (
          <SleeveCard
            key={sleeve}
            sleeve={sleeve}
            scan={scanOf(today, sleeve)}
            role={today.roles.find((role) => role.sleeve === sleeve)}
          />
        ))}
      </div>
    </section>
  );
}

export default async function OptionsPage() {
  const [today, chain] = await Promise.all([
    fetchOptionsToday(),
    fetchOptionsChain(),
  ]);
  const clock = today ? clockLabel(today) : null;
  const empty = today ? emptyReasonText(today.empty_reason) : null;

  return (
    <div className="space-y-8">
      <PageHeader
        title={PAGES["/options"].title}
        blurb={PAGES["/options"].blurb}
        meta={
          <span className="flex flex-wrap items-center gap-2">
            {clock ? (
              <ToneBadge
                text={clock.text}
                tone={clock.tone}
                testId="options-clock"
              />
            ) : null}
            <ScanOnly />
          </span>
        }
      />
      <OptionsSubNav />
      <SectionTabs section="options" />

      <FnoRiskCaveat />

      <Answer
        footnote={
          "Scans are read from the options collector's latest minute while the market is open, " +
          "and from the last scanned session's close otherwise. Every candidate is a paper one — " +
          "a plan is confirmed on the desk, by hand, and nothing on this page can place an order."
        }
      >
        {today === null ? (
          <>
            The options service did not answer, so this page has nothing to show
            right now.
          </>
        ) : empty ? (
          <span data-testid="options-empty-reason">{empty}</span>
        ) : (
          <>
            Today&rsquo;s scan: <Mark>{summary(today)}</Mark>.
          </>
        )}
      </Answer>

      {today ? (
        <>
          <HeaderStrip today={today} />
          <Group
            title="Premium selling"
            sleeves={["O1M", "O1W"]}
            today={today}
          />
          <Group title="Directional" sleeves={["O2"]} today={today} />
          <Group
            title="Expiry-day setups"
            sleeves={["O3B", "O3A"]}
            today={today}
          />
          <PositionsPanel
            positions={today.positions}
            closed={today.closed_today}
            weekR={today.week_r}
          />
        </>
      ) : null}

      <ChainPanel chain={chain} />
    </div>
  );
}
