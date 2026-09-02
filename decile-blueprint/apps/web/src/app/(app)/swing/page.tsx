import type { Metadata } from "next";

import { Answer, Mark } from "@/components/shell/answer";
import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { CatalystLink } from "@/components/swing/catalyst-link";
import { formatTradeDate } from "@/lib/format";
import { fetchSectors, fetchSetups, type SwingSetup } from "@/lib/swing/fetch";
import { PAGES } from "@/lib/vocabulary";

/**
 * `/swing` — the Setups tab of `docs/swing/05` §2.
 *
 * The product test of the whole SW run, in one sentence from `docs/swing/README.md`: *"Maulik
 * opens the web app on a weekend and sees the flags forming with their pivots."* So the page
 * answers three questions in the order a person asks them — is the tape worth trading, which
 * names are ready, and where would each one break out — and it answers the first one before the
 * list, because a list of setups above a red gate is a list of trades not to take.
 *
 * **Read-only.** There is no form, no action and no server action on this page. A swing line
 * becomes an order in the desk console, on a click, and nowhere else
 * (`docs/swing/02-scope-and-gating.md` Track C §4). `__tests__/read-only.test.ts` asserts it.
 */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/swing"].title,
  description: PAGES["/swing"].blurb,
};

/** `04` §2-§4's three setups, in the order the page lists them. */
const SETUP_SECTIONS = [
  {
    key: "FLAG",
    heading: "Flags",
    blurb:
      "A leader that ran, then rested. The trigger is the top of the base; the stop is the low " +
      "of the day it breaks out.",
  },
  {
    key: "EP",
    heading: "Episodic pivots",
    blurb:
      "A gap out of a long quiet stretch, on volume. His biggest winners, and the ones that " +
      "fail fastest if the gap does not hold.",
  },
  {
    key: "PARABOLIC_SHORT",
    heading: "Parabolic — for the record. Not tradeable on NSE delivery.",
    blurb:
      "Names that have gone vertical. Kept as a froth gauge and a do-not-chase list: NSE cash " +
      "equities cannot be shorted for delivery, so none of these is ever a plan line.",
  },
] as const;

const GATE_COPY: Record<string, string> = {
  GREEN: "breakouts are working — the allocation may press",
  AMBER: "mixed — entries allowed, at the size the rung permits",
  RED: "no new entries; manage what is open",
};

function money(value: number | null): string {
  return value === null ? "—" : value.toFixed(2);
}

function percent(value: number | null): string {
  return value === null ? "—" : `${value.toFixed(2)}%`;
}

function crore(value: number | null): string {
  return value === null ? "—" : `₹${(value / 10_000_000).toFixed(1)} cr`;
}

/**
 * The empty state, written from the funnel — `05` §2's "No flags today — 41 names were liquid,
 * 0 met the base rules".
 *
 * Without the counts, an empty list and a job that never ran render identically, and those need
 * opposite responses from the person reading it.
 */
function emptyReason(
  heading: string,
  funnel: { liquid?: number; instruments?: number } | null,
): string {
  if (!funnel) {
    return `No ${heading.toLowerCase()} — and no scan has run yet, so this is not a statement about the market.`;
  }
  const liquid = funnel.liquid ?? 0;
  const universe = funnel.instruments ?? 0;
  return `No ${heading.toLowerCase()} today — ${universe.toLocaleString("en-IN")} names had a bar, ${liquid.toLocaleString("en-IN")} of them were liquid enough, and none met the rules.`;
}

function SetupTable({ rows }: { rows: SwingSetup[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[52rem] border-collapse text-sm">
        <thead>
          <tr className="border-b border-border/70 text-left text-xs uppercase tracking-wide text-muted-foreground">
            <th className="py-2 pr-3 font-medium">Stock</th>
            <th className="py-2 pr-3 font-medium">Score</th>
            <th className="py-2 pr-3 font-medium">Close</th>
            <th className="py-2 pr-3 font-medium">Trigger</th>
            <th className="py-2 pr-3 font-medium">Stop</th>
            <th className="py-2 pr-3 font-medium">Risk</th>
            <th className="py-2 pr-3 font-medium">ADR</th>
            <th className="py-2 pr-3 font-medium">Turnover</th>
            <th className="py-2 pr-3 font-medium">Sector</th>
            <th className="py-2 pr-3 font-medium">Catalyst</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={`${row.setup}-${row.instrument_id}`}
              className="border-b border-border/40"
            >
              <td className="py-2 pr-3">
                <span className="font-medium">{row.symbol}</span>
                <span className="ml-2 text-xs text-muted-foreground">
                  {row.name}
                </span>
                {row.locked_upper_circuit ? (
                  <span
                    className="ml-2 text-xs text-amber-600"
                    title="Locked at its upper circuit — there was no seller, so a break here is not a fill."
                  >
                    locked
                  </span>
                ) : null}
                {row.listed_within_2y ? (
                  <span
                    className="ml-2 text-xs text-muted-foreground"
                    title="Listed within two years — a young stock, which he prefers."
                  >
                    new
                  </span>
                ) : null}
              </td>
              <td className="py-2 pr-3 tabular-nums">{row.score.toFixed(0)}</td>
              <td className="py-2 pr-3 tabular-nums">{money(row.close)}</td>
              <td className="py-2 pr-3 tabular-nums font-medium">
                {money(row.trigger)}
              </td>
              <td className="py-2 pr-3 tabular-nums">{money(row.stop_ref)}</td>
              <td className="py-2 pr-3 tabular-nums">
                {percent(row.stop_distance_pct)}
              </td>
              <td className="py-2 pr-3 tabular-nums">{percent(row.adr_pct)}</td>
              <td className="py-2 pr-3 tabular-nums">
                {crore(row.turnover_avg)}
              </td>
              <td className="py-2 pr-3 text-xs text-muted-foreground">
                {row.sector_slug ?? "—"}
              </td>
              <td className="py-2 pr-3 text-xs text-muted-foreground">
                <CatalystLink feed={row.catalyst_feed} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default async function SwingSetupsPage() {
  const [setups, sectors] = await Promise.all([
    fetchSetups({}),
    fetchSectors(),
  ]);

  const rows = setups?.data ?? [];
  const gate = setups?.gate ?? null;
  const asOf = setups?.as_of ?? null;

  return (
    <div className="space-y-8">
      <PageHeader
        title={PAGES["/swing"].title}
        blurb={PAGES["/swing"].blurb}
        meta={
          asOf ? (
            <span className="text-sm text-muted-foreground">
              As of {formatTradeDate(asOf)}
            </span>
          ) : null
        }
      />
      <SectionTabs section="swing" />

      <Answer
        footnote={
          "Detected from published end-of-day bars. Nothing here is advice, and nothing on " +
          "this page can place an order — a line becomes an order in the desk console, on a click."
        }
      >
        {gate ? (
          <>
            The tape is <Mark>{gate}</Mark> —{" "}
            {GATE_COPY[gate] ?? "read the market tab"}. The allocation may hold
            up to <Mark>{setups?.max_open_positions ?? 0} positions</Mark> and{" "}
            <Mark>
              {(setups?.max_exposure_pct ?? 0).toFixed(0)}% of the allocation
            </Mark>
            , which is rung {(setups?.exposure_level ?? 0) + 1} of 4.
          </>
        ) : (
          <>No scan has run yet, so there is nothing to say about today.</>
        )}
      </Answer>

      {sectors && sectors.data.length > 0 ? (
        <section aria-label="Sectors" className="space-y-2">
          <h2 className="text-sm font-medium uppercase tracking-wide text-muted-foreground">
            Where the strength is
          </h2>
          <ul className="flex flex-wrap gap-2">
            {sectors.data.map((sector) => (
              <li
                key={sector.slug}
                className={
                  sector.hot
                    ? "rounded-md border border-border bg-muted/60 px-3 py-1.5 text-sm"
                    : "rounded-md border border-border/50 px-3 py-1.5 text-sm text-muted-foreground"
                }
              >
                <span className="font-medium">
                  {sector.slug.replace(/^nifty-/, "")}
                </span>{" "}
                <span className="tabular-nums">
                  {sector.pct_above_ma_slow.toFixed(0)}%
                </span>
                <span className="ml-2 text-xs">
                  {sector.candidates}{" "}
                  {sector.candidates === 1 ? "candidate" : "candidates"}
                </span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {SETUP_SECTIONS.map((section) => {
        const sectionRows = rows.filter((row) => row.setup === section.key);
        return (
          <section
            key={section.key}
            aria-label={section.heading}
            className="space-y-3"
          >
            <div className="space-y-1">
              <h2 className="text-lg font-medium">{section.heading}</h2>
              <p className="max-w-[70ch] text-sm text-muted-foreground">
                {section.blurb}
              </p>
            </div>
            {sectionRows.length > 0 ? (
              <SetupTable rows={sectionRows} />
            ) : (
              <p className="text-sm text-muted-foreground">
                {emptyReason(section.heading, setups?.funnel ?? null)}
              </p>
            )}
          </section>
        );
      })}
    </div>
  );
}
