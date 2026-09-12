import type { Metadata, Route } from "next";
import Link from "next/link";

import { Answer, Mark } from "@/components/shell/answer";
import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { CatalystLink } from "@/components/swing/catalyst-link";
import { formatTradeDate } from "@/lib/format";
import {
  fetchBars,
  fetchMarket,
  fetchSectors,
  fetchSetups,
  fetchWatchlist,
  type SwingBar,
  type SwingMarketDay,
  type SwingSetup,
  type SwingWatchRow,
} from "@/lib/swing/fetch";
import { PAGES } from "@/lib/vocabulary";

import { scanNow, watchAdd, watchDismiss } from "./actions";
import { RowActionForm } from "./_components/forms";
import { MiniChart } from "./_components/mini-chart";
import { ScanLabel, ScanNow } from "./_components/scan-now";
import { breadthLine, gateCopy, tierLine } from "./copy";

/**
 * `/swing` — the Setups tab of `docs/swing/05` §2.
 *
 * The product test of the whole SW run, in one sentence from `docs/swing/README.md`: *"Maulik
 * opens the web app on a weekend and sees the flags forming with their pivots."* So the page
 * answers three questions in the order a person asks them — is the tape worth trading, which
 * names are ready, and where would each one break out — and it answers the first one before the
 * list, because a list of setups above a red gate is a list of trades not to take.
 *
 * Two row actions, and only two (SW14): **Watch** puts a candidate on `sw_watch`, **Dismiss**
 * marks the row that is already there DISMISSED. Neither moves money (`02` Track A). A swing
 * line becomes an order in the desk console, on a click, and nowhere else (Track C §4);
 * `__tests__/read-only.test.tsx` asserts it over every action this tree can name.
 *
 * One page action (SW15): **Scan now** queues the detectors. During the session the worker
 * builds today's bar from live Kite quotes and every row it writes is labelled provisional —
 * the header says "provisional — scanned 13:42 IST from live quotes" so nobody reads a 13:42
 * base as a close. The nightly replaces those rows.
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
    shape: "Base bars",
  },
  {
    key: "EP",
    heading: "Episodic pivots",
    blurb:
      "A gap out of a long quiet stretch, on volume. His biggest winners, and the ones that " +
      "fail fastest if the gap does not hold.",
    shape: "Gap",
  },
  {
    key: "PARABOLIC_SHORT",
    heading: "Parabolic — for the record. Not tradeable on NSE delivery.",
    blurb:
      "Names that have gone vertical. Kept as a froth gauge and a do-not-chase list: NSE cash " +
      "equities cannot be shorted for delivery, so none of these is ever a plan line.",
    shape: "Streak",
  },
] as const;

/** The detector's statuses, as the filter chips name them. */
const STATUS_CHIPS: { key: string; label: string }[] = [
  { key: "", label: "Every status" },
  { key: "SETTING_UP", label: "Setting up" },
  { key: "TRIGGERED", label: "Triggered" },
  { key: "EXTENDED", label: "Extended" },
  { key: "FAILED", label: "Failed" },
];

/** How many rows get a chart. Each is one request to the API, made in parallel, bounded here. */
const CHART_ROWS = 24;

function money(value: number | null): string {
  return value === null ? "—" : value.toFixed(2);
}

function percent(value: number | null): string {
  return value === null ? "—" : `${value.toFixed(2)}%`;
}

function crore(value: number | null): string {
  return value === null ? "—" : `₹${(value / 10_000_000).toFixed(1)} cr`;
}

function shape(row: SwingSetup): string {
  if (row.setup === "FLAG") return row.base_bars === null ? "—" : `${row.base_bars} bars`;
  if (row.setup === "EP") return percent(row.gap_pct);
  return row.up_streak === null ? "—" : `${row.up_streak} up`;
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
  setupKey: string,
  funnel: { liquid?: number; instruments?: number; candidates?: Record<string, number> } | null,
  status: string,
): string {
  const what = heading.split(" — ")[0]?.toLowerCase() ?? heading.toLowerCase();
  if (!funnel) {
    return `No ${what} — and no scan has run yet, so this is not a statement about the market.`;
  }
  const liquid = funnel.liquid ?? 0;
  const universe = funnel.instruments ?? 0;
  const candidates = funnel.candidates?.[setupKey] ?? 0;
  if (status && candidates > 0) {
    return `No ${what} with that status today — ${candidates} met the rules; clear the filter to see them.`;
  }
  return `No ${what} today — ${universe.toLocaleString("en-IN")} names had a bar, ${liquid.toLocaleString("en-IN")} of them were liquid enough, and ${candidates} met the rules.`;
}

function StatusPill({ status }: { status: string }) {
  const tone =
    status === "TRIGGERED"
      ? "border-positive/50 text-positive"
      : status === "FAILED" || status === "EXTENDED"
        ? "border-border text-muted-foreground"
        : "border-border text-foreground";
  return (
    <span className={`rounded border px-1.5 py-0.5 text-[11px] uppercase tracking-wide ${tone}`}>
      {status.replaceAll("_", " ").toLowerCase()}
    </span>
  );
}

function SetupTable({
  rows,
  shapeLabel,
  watched,
  charts,
  tradeable,
}: {
  rows: SwingSetup[];
  shapeLabel: string;
  watched: Map<number, SwingWatchRow>;
  charts: Map<number, SwingBar[]>;
  tradeable: boolean;
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[64rem] border-collapse text-sm">
        <thead>
          <tr className="border-b border-border/70 text-left text-xs uppercase tracking-wide text-muted-foreground">
            <th className="py-2 pr-3 font-medium">Stock</th>
            <th className="py-2 pr-3 font-medium">Status</th>
            <th className="py-2 pr-3 font-medium">Score</th>
            <th className="py-2 pr-3 font-medium">Close</th>
            <th className="py-2 pr-3 font-medium">Trigger</th>
            <th className="py-2 pr-3 font-medium">Stop</th>
            <th className="py-2 pr-3 font-medium">Risk</th>
            <th className="py-2 pr-3 font-medium">ADR</th>
            <th className="py-2 pr-3 font-medium">Turnover</th>
            <th className="py-2 pr-3 font-medium">{shapeLabel}</th>
            <th className="py-2 pr-3 font-medium">Sector</th>
            <th className="py-2 pr-3 font-medium">Chart</th>
            <th className="py-2 pr-3 font-medium">Catalyst</th>
            {tradeable ? <th className="py-2 pr-3 font-medium">Action</th> : null}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const watch = watched.get(row.instrument_id);
            const bars = charts.get(row.instrument_id);
            return (
              <tr
                key={`${row.setup}-${row.instrument_id}`}
                className={
                  watch?.focus
                    ? "border-b border-border/40 bg-muted/40"
                    : "border-b border-border/40"
                }
                data-focus={watch?.focus ? "true" : undefined}
              >
                <td className="py-2 pr-3">
                  {watch?.focus ? (
                    <span
                      className="mr-1.5 text-warning"
                      title="In today's focus — the top five flags by score and every episodic pivot. Pushed at the trigger and first on the desk page."
                      aria-label="focus"
                    >
                      ★
                    </span>
                  ) : null}
                  <span className="font-medium">{row.symbol}</span>
                  <span className="ml-2 text-xs text-muted-foreground">{row.name}</span>
                  {row.locked_upper_circuit ? (
                    <span
                      className="ml-2 text-xs text-warning"
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
                <td className="py-2 pr-3">
                  <StatusPill status={row.status} />
                </td>
                <td className="py-2 pr-3 tabular-nums">{row.score.toFixed(0)}</td>
                <td className="py-2 pr-3 tabular-nums">{money(row.close)}</td>
                <td className="py-2 pr-3 tabular-nums font-medium">{money(row.trigger)}</td>
                <td className="py-2 pr-3 tabular-nums">{money(row.stop_ref)}</td>
                <td className="py-2 pr-3 tabular-nums">{percent(row.stop_distance_pct)}</td>
                <td className="py-2 pr-3 tabular-nums">{percent(row.adr_pct)}</td>
                <td className="py-2 pr-3 tabular-nums">{crore(row.turnover_avg)}</td>
                <td className="py-2 pr-3 tabular-nums">{shape(row)}</td>
                <td className="py-2 pr-3 text-xs text-muted-foreground">{row.sector_slug ?? "—"}</td>
                <td className="py-1 pr-3">
                  {bars ? (
                    <MiniChart
                      bars={bars}
                      trigger={row.trigger}
                      stop={row.stop_ref}
                      label={`${row.symbol}: the last ${bars.length} closes with the 10- and 20-day averages, the trigger and the stop`}
                    />
                  ) : (
                    <span className="text-xs text-muted-foreground">—</span>
                  )}
                </td>
                <td className="py-2 pr-3 text-xs text-muted-foreground">
                  <CatalystLink feed={row.catalyst_feed} />
                </td>
                {tradeable ? (
                  <td className="py-2 pr-3">
                    {watch ? (
                      <span className="inline-flex flex-wrap items-center gap-2">
                        <span className="text-xs text-muted-foreground">
                          watching
                          {watch.source === "MANUAL" ? " (yours)" : ""}
                        </span>
                        <RowActionForm
                          action={watchDismiss}
                          fields={{ id: watch.id }}
                          label="Dismiss"
                          pendingLabel="Dismissing…"
                          title="Mark the watchlist row dismissed. It is kept, as the record of a no."
                          variant="ghost"
                        />
                      </span>
                    ) : (
                      <RowActionForm
                        action={watchAdd}
                        fields={{
                          instrument_id: row.instrument_id,
                          symbol: row.symbol,
                          setup: row.setup,
                          trigger: row.trigger === null ? "" : row.trigger.toFixed(2),
                          stop_ref: row.stop_ref === null ? "" : row.stop_ref.toFixed(2),
                        }}
                        label="Watch"
                        pendingLabel="Adding…"
                        title="Put this name on the watchlist with these levels. Moves no money."
                      />
                    )}
                  </td>
                ) : null}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

async function chartsFor(rows: SwingSetup[], asOf: string | null): Promise<Map<number, SwingBar[]>> {
  const wanted = [...rows]
    .sort((a, b) => b.score - a.score)
    .slice(0, CHART_ROWS)
    .map((row) => row.instrument_id);
  const series = await Promise.all(
    wanted.map((id) => fetchBars(id, asOf ?? undefined).catch(() => null)),
  );
  const charts = new Map<number, SwingBar[]>();
  wanted.forEach((id, index) => {
    const bars = series[index]?.data;
    if (bars && bars.length > 1) charts.set(id, bars);
  });
  return charts;
}

export default async function SwingSetupsPage(props: {
  searchParams?: Promise<{ status?: string }>;
}) {
  const params = (await props.searchParams) ?? {};
  const status = STATUS_CHIPS.some((chip) => chip.key === params.status && chip.key)
    ? (params.status ?? "")
    : "";

  const [setups, sectors, watchlist] = await Promise.all([
    fetchSetups(status ? { status } : {}),
    fetchSectors(),
    fetchWatchlist(),
  ]);
  const asOf = setups?.as_of ?? null;
  const [marketDays, charts] = await Promise.all([
    asOf ? fetchMarket({ from: asOf, to: asOf }) : Promise.resolve(null),
    chartsFor(setups?.data ?? [], asOf),
  ]);
  const day: SwingMarketDay | null = marketDays?.data.at(-1) ?? null;

  const rows = setups?.data ?? [];
  const gate = setups?.gate ?? null;
  const provisional = setups?.as_of_provisional ?? false;
  const scannedAt = setups?.scanned_at ?? null;
  const lastScan = setups?.last_scan ?? null;
  const watched = new Map<number, SwingWatchRow>();
  for (const row of watchlist?.data ?? []) watched.set(row.instrument_id, row);
  const focusCount = rows.filter((row) => watched.get(row.instrument_id)?.focus).length;

  return (
    <div className="space-y-8">
      <PageHeader
        title={PAGES["/swing"].title}
        blurb={PAGES["/swing"].blurb}
        meta={
          <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
            {asOf ? (
              <span className="text-sm text-muted-foreground">As of {formatTradeDate(asOf)}</span>
            ) : null}
            <ScanLabel provisional={provisional} scannedAt={scannedAt} />
            <ScanNow action={scanNow} lastScan={lastScan} session={asOf} />
          </span>
        }
      />
      <SectionTabs section="swing" />

      <Answer
        footnote={
          (provisional
            ? "Detected on today's bar so far, built from live quotes — provisional until the " +
              "nightly scan replaces it. "
            : "Detected from published end-of-day bars. ") +
          "Nothing here is advice, and nothing on this page can place an order — a line " +
          "becomes an order in the desk console, on a click."
        }
      >
        {gate ? (
          <>
            The tape is <Mark>{gate}</Mark> — {gateCopy(gate)}.{" "}
            {day ? (
              <>
                <Mark>{tierLine(day)}</Mark>.
              </>
            ) : (
              <>
                The allocation may hold up to <Mark>{setups?.max_open_positions ?? 0} positions</Mark>{" "}
                and <Mark>{(setups?.max_exposure_pct ?? 0).toFixed(0)}% of the allocation</Mark>, which
                is rung {(setups?.exposure_level ?? 0) + 1} of 4.
              </>
            )}
          </>
        ) : (
          <>No scan has run yet, so there is nothing to say about today.</>
        )}
      </Answer>

      {day ? (
        <p className="max-w-[80ch] text-sm text-muted-foreground" data-testid="gate-detail">
          <span className="font-medium text-foreground">{day.gate}</span> because {breadthLine(day)}.
          {focusCount > 0 ? (
            <>
              {" "}
              <span className="text-warning">★</span> marks the {focusCount} in today&rsquo;s
              focus — the top five flags by score and every episodic pivot.
            </>
          ) : null}
        </p>
      ) : null}

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
                <span className="font-medium">{sector.slug.replace(/^nifty-/, "")}</span>{" "}
                <span className="tabular-nums">{sector.pct_above_ma_slow.toFixed(0)}%</span>
                <span className="ml-2 text-xs">
                  {sector.candidates} {sector.candidates === 1 ? "candidate" : "candidates"}
                </span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <nav aria-label="Filter by status" className="flex flex-wrap gap-2">
        {STATUS_CHIPS.map((chip) => (
          <Link
            key={chip.key || "all"}
            href={(chip.key ? `/swing?status=${chip.key}` : "/swing") as Route}
            aria-current={status === chip.key ? "page" : undefined}
            className={
              status === chip.key
                ? "rounded-full border border-foreground bg-foreground px-3 py-1 text-xs text-background"
                : "rounded-full border border-border px-3 py-1 text-xs text-muted-foreground hover:border-foreground/60"
            }
          >
            {chip.label}
          </Link>
        ))}
      </nav>

      {SETUP_SECTIONS.map((section) => {
        const sectionRows = rows
          .filter((row) => row.setup === section.key)
          .sort((a, b) => b.score - a.score);
        return (
          <section key={section.key} aria-label={section.heading} className="space-y-3">
            <div className="space-y-1">
              <h2 className="text-lg font-medium">{section.heading}</h2>
              <p className="max-w-[70ch] text-sm text-muted-foreground">{section.blurb}</p>
            </div>
            {sectionRows.length > 0 ? (
              <SetupTable
                rows={sectionRows}
                shapeLabel={section.shape}
                watched={watched}
                charts={charts}
                tradeable={section.key !== "PARABOLIC_SHORT"}
              />
            ) : (
              <p className="text-sm text-muted-foreground">
                {emptyReason(section.heading, section.key, setups?.funnel ?? null, status)}
              </p>
            )}
          </section>
        );
      })}
    </div>
  );
}
