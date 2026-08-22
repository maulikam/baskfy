import type { Metadata } from "next";

import {
  DeskNav,
  Empty,
  Notice,
  PageHeader,
  ReadOnlyFooter,
  Stat,
  StatRow,
  pct,
} from "@/components/desk/ui";
import { DeskUnavailable, fetchRegime } from "@/lib/desk/fetch";
import { PAGES } from "@/lib/vocabulary";

/**
 * `/regime` — M26. How defensive the strategy is being, and why.
 *
 * The desk stores the reasoning as sentences it wrote at evaluation time, so this page mostly
 * gets out of the way and prints them. That is deliberate: an explanation the machine wrote
 * when it decided is worth more than one a page assembles afterwards.
 *
 * **Read-only.** The stance is evaluated by the desk on its own schedule.
 */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/regime"].title,
  description: "How defensive the strategy is being right now, and the reasons it gave.",
};

/** The desk's tiers, in a sentence rather than a code. */
const TIERS: Record<string, { label: string; meaning: string }> = {
  R1: { label: "Risk-on", meaning: "Fully invested. New positions open at full size." },
  R2: { label: "Cautious", meaning: "Still invested, but new positions open at reduced size." },
  R3: { label: "Defensive", meaning: "Exposure is being reduced. New positions are not opened." },
  R4: { label: "Risk-off", meaning: "Out of the market, or heading there." },
};

const BUYS: Record<string, string> = {
  full: "New positions open at full size.",
  half: "New positions open at half size.",
  none: "No new positions are being opened.",
};

export default async function RegimePage() {
  let data;
  try {
    data = await fetchRegime();
  } catch (error) {
    if (!(error instanceof DeskUnavailable)) throw error;
    return (
      <Empty
        title={PAGES["/regime"].title}
        body="The desk has not evaluated the market stance yet. It does so weekly."
      />
    );
  }

  const tier = TIERS[data.tier] ?? { label: data.tier, meaning: "" };

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        title={PAGES["/regime"].title}
        lede={PAGES["/regime"].blurb}
        meta={`Last worked out ${data.evaluated_at.slice(0, 10)}${data.signal_date ? `, from ${data.signal_date} closing prices` : ""}.`}
      />

      <section className="rounded-xl border border-border/70 bg-card p-5">
        <div className="eyebrow">Where the dial is now</div>
        <div className="mt-1 text-2xl font-semibold">
          {tier.label} <span className="text-base font-normal text-muted-foreground">({data.tier})</span>
        </div>
        {tier.meaning && <p className="mt-1 text-sm text-muted-foreground">{tier.meaning}</p>}
        {data.previous_tier && data.previous_tier !== data.tier && (
          <p className="mt-1 text-sm text-muted-foreground">Changed from {data.previous_tier}.</p>
        )}
      </section>

      {data.manual_action_required && (
        <Notice>
          This evaluation is flagged as needing a person to look at it. Open the desk console.
        </Notice>
      )}
      {data.data_stale && (
        <Notice>
          The evaluation ran on data it considered stale, so treat the stance as provisional.
        </Notice>
      )}

      <StatRow>
        <Stat
          label="How many are joining in"
          value={pct(data.breadth_pct)}
          hint="above their one-month trend"
        />
        <Stat label="In shares right now" value={pct(data.actual_equity_pct)} hint="of the portfolio" />
        <Stat label="Most it may hold" value={pct(data.target_equity_cap_pct)} hint="at this setting on the dial" />
        <Stat
          label="New buys allowed"
          value={data.new_buys ? (data.new_buys === "full" ? "Full size" : data.new_buys === "half" ? "Half size" : "None") : "–"}
        />
      </StatRow>

      {data.new_buys && BUYS[data.new_buys] && (
        <p className="text-sm text-muted-foreground">{BUYS[data.new_buys]}</p>
      )}

      <section className="flex flex-col gap-2">
        <h2 className="text-base">Why it is set there</h2>
        <ul className="flex flex-col gap-1.5 text-sm">
          {data.reasons.map((reason) => (
            <li key={reason} className="flex gap-2">
              <span aria-hidden className="text-muted-foreground">
                •
              </span>
              <span>{reason}</span>
            </li>
          ))}
        </ul>
        {data.reasons.length === 0 && (
          <p className="text-sm text-muted-foreground">This evaluation recorded no reasons.</p>
        )}
      </section>

      {data.next_evaluation_date && (
        <p className="text-xs text-muted-foreground">
          Next evaluation: {data.next_evaluation_date}.
          {data.mode === "observe" && " The desk is observing only — this stance is not forcing any selling."}
        </p>
      )}

      <DeskNav current="/regime" />
      <ReadOnlyFooter />
    </div>
  );
}
