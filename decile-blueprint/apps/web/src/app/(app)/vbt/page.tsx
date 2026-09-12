import type { Metadata } from "next";

import { Answer, Mark } from "@/components/shell/answer";
import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { formatTradeDate } from "@/lib/format";
import {
  fetchBars,
  fetchBreadth,
  fetchLastScan,
  fetchToday,
  type VbtCandidate,
} from "@/lib/vbt/fetch";
import { PAGES } from "@/lib/vocabulary";

import { scanNow } from "./actions";
import { BreadthGauge } from "./_components/breadth-gauge";
import { MiniChart, type ChartBar } from "./_components/mini-chart";
import { ScanNow } from "./_components/scan-now";
import { breadthLine, funnelLine, gateCopy, shutLine } from "./copy";

/**
 * `/vbt` — the Today tab of `docs/vbt/05` §2.
 *
 * The page answers three questions in the order a person asks them: **may the sleeve buy at all**
 * (the gate), **which names printed a real breakout**, and **what the scan threw away**. The gate
 * comes first because a list of candidates above a shut gate is a list of trades not to take, and
 * `01` §3's ablation says the gate is worth more than any single filter.
 *
 * **Nothing on this page can place an order.** There is exactly one server action under this
 * tree — "Scan now", which queues the detector and moves no money — and `docs/vbt/02` Track C §4
 * still gives the web app no route under `/vbt` that can reach the gateway: this sleeve confirms
 * every order by hand in the desk console. `__tests__/read-only.test.tsx` is the census that
 * keeps the set at exactly one (DECISIONS-VB VB14, which supersedes VB8.4's "no action at all").
 *
 * `05` §2 also sketched a per-row **Dismiss** note. It is not built: the data model (`03`) has no
 * table to put a note in, and inventing one to hold a UI affordance nobody has asked for would
 * have been a migration in service of a mock-up. Recorded in DECISIONS-VB VB8.4.
 */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/vbt"].title,
  description: PAGES["/vbt"].blurb,
};

/** How many rows get a chart. Each is one request, made in parallel, bounded here. */
const CHART_ROWS = 20;

function money(value: number | null): string {
  return value === null ? "—" : value.toFixed(2);
}

function percent(value: number | null, places = 1): string {
  return value === null ? "—" : `${value.toFixed(places)}%`;
}

function crore(value: number | null): string {
  return value === null ? "—" : `₹${(value / 10_000_000).toFixed(1)} cr`;
}

/** `04` §3.1's close position: 0.6 is the floor, and the page shows it as a fraction of range. */
function position(value: number | null): string {
  return value === null ? "—" : value.toFixed(2);
}

async function chartsFor(
  rows: VbtCandidate[],
  asOf: string | null,
): Promise<Map<number, ChartBar[]>> {
  const wanted = rows.slice(0, CHART_ROWS);
  const results = await Promise.all(
    wanted.map(async (row) => {
      const bars = await fetchBars(row.instrument_id, asOf ?? undefined);
      return [row.instrument_id, bars?.data ?? []] as const;
    }),
  );
  return new Map(results.filter(([, bars]) => bars.length > 0));
}

function CandidateTable({
  rows,
  charts,
}: {
  rows: VbtCandidate[];
  charts: Map<number, ChartBar[]>;
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[72rem] border-collapse text-sm">
        <thead>
          <tr className="border-b border-border/70 text-left text-xs uppercase tracking-wide text-muted-foreground">
            <th className="py-2 pr-3 font-medium">Stock</th>
            <th className="py-2 pr-3 font-medium">Close</th>
            <th className="py-2 pr-3 font-medium">Limit</th>
            <th className="py-2 pr-3 font-medium">Stop</th>
            <th className="py-2 pr-3 font-medium">Change</th>
            <th className="py-2 pr-3 font-medium">Rel. volume</th>
            <th className="py-2 pr-3 font-medium">Close position</th>
            <th className="py-2 pr-3 font-medium">20-day return</th>
            <th className="py-2 pr-3 font-medium">Turnover</th>
            <th className="py-2 pr-3 font-medium">Above 200-DMA</th>
            <th className="py-2 pr-3 font-medium">Chart</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.instrument_id} className="border-b border-border/40">
              <td className="py-2 pr-3">
                <span className="font-medium">{row.symbol}</span>
                {row.locked_upper_circuit ? (
                  <span
                    title="Locked at the upper circuit — no plan line is placed for it (04 §7.4)"
                    className="ml-1.5 text-warning"
                    data-testid="locked-warning"
                  >
                    ⚠
                  </span>
                ) : null}
                <span className="block text-xs text-muted-foreground">
                  {row.name}
                </span>
              </td>
              <td className="py-2 pr-3 tabular-nums">{money(row.close)}</td>
              <td className="py-2 pr-3 font-medium tabular-nums">
                {money(row.limit_price)}
              </td>
              <td className="py-2 pr-3 tabular-nums">
                {money(row.stop_price)}
              </td>
              <td className="py-2 pr-3 tabular-nums">
                {percent(row.change_pct, 2)}
              </td>
              <td className="py-2 pr-3 tabular-nums">
                {row.rvol === null ? "—" : `${row.rvol.toFixed(1)}×`}
              </td>
              <td className="py-2 pr-3 tabular-nums">
                {position(row.close_position)}
              </td>
              <td className="py-2 pr-3 tabular-nums">
                {percent(row.ret_20_pct)}
              </td>
              <td className="py-2 pr-3 tabular-nums">
                {crore(row.turnover_avg_20)}
              </td>
              <td className="py-2 pr-3 tabular-nums">
                {percent(row.pct_above_dma)}
              </td>
              <td className="py-2 pr-3">
                <MiniChart
                  bars={charts.get(row.instrument_id) ?? []}
                  limit={row.limit_price}
                  stop={row.stop_price}
                  priorHigh={row.high_20_prior}
                  label={`${row.symbol}: 130 sessions of adjusted closes, with its limit and stop`}
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default async function VbtTodayPage() {
  const today = await fetchToday();
  const asOf = today?.as_of ?? null;
  const [breadth, charts, lastScan] = await Promise.all([
    fetchBreadth(),
    chartsFor(today?.candidates ?? [], asOf),
    fetchLastScan(today),
  ]);

  const candidates = today?.candidates ?? [];
  const rejects = today?.rejects ?? [];
  const gate = today?.gate ?? null;

  return (
    <div className="space-y-8">
      <PageHeader
        title={PAGES["/vbt"].title}
        blurb={PAGES["/vbt"].blurb}
        meta={
          <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
            {asOf ? (
              <span className="text-sm text-muted-foreground">
                As of {formatTradeDate(asOf)}
              </span>
            ) : null}
            <ScanNow action={scanNow} lastScan={lastScan} session={asOf} />
          </span>
        }
      />
      <SectionTabs section="vbt" />

      <Answer
        footnote={
          "Detected from published end-of-day bars — a daily bar is a closed day, so this is the " +
          "last completed session and not today. Nothing here is advice, and nothing on this " +
          "page can place an order: a line becomes an order in the desk console, on a click."
        }
      >
        {gate ? (
          <>
            The gate is <span data-testid="gate-badge">
              <Mark>{gate}</Mark>
            </span> &mdash;{" "}
            {gateCopy(gate)}. <Mark>{breadthLine(today!)}</Mark>.
          </>
        ) : (
          <>
            No detection has run yet, so there is nothing to say about this
            strategy.
          </>
        )}
      </Answer>

      {today ? (
        <p
          className="max-w-[80ch] text-sm text-muted-foreground"
          data-testid="funnel-line"
        >
          {funnelLine(today)}. {shutLine(today)}
        </p>
      ) : null}

      {breadth && breadth.data.length > 0 ? (
        <section aria-label="Breadth" className="space-y-2">
          <h2 className="text-sm font-medium uppercase tracking-wide text-muted-foreground">
            How much of the market is above its own 200-day average
          </h2>
          <BreadthGauge data={breadth.data} threshold={breadth.threshold_pct} />
        </section>
      ) : null}

      <section aria-label="Candidates" className="space-y-2">
        <h2 className="text-sm font-medium uppercase tracking-wide text-muted-foreground">
          Today&rsquo;s candidates
        </h2>
        {candidates.length > 0 ? (
          <CandidateTable rows={candidates} charts={charts} />
        ) : (
          <p
            className="text-sm text-muted-foreground"
            data-testid="candidates-empty"
          >
            {today ? funnelLine(today) : "No scan has run yet."}
          </p>
        )}
      </section>

      {rejects.length > 0 ? (
        <details className="rounded-lg border border-border/60 p-4">
          <summary className="cursor-pointer text-sm font-medium">
            What the scan found and the filters rejected (
            {rejects.length.toLocaleString("en-IN")})
          </summary>
          <p className="mt-2 max-w-[80ch] text-sm text-muted-foreground">
            These met all five volume-scan lines and failed at least one trend
            filter. They are here because the ablation table is the argument for
            the filters, and a page that never shows the rejects makes that
            argument unreadable.
          </p>
          <ul className="mt-3 flex flex-wrap gap-2" data-testid="rejects">
            {rejects.map((row) => (
              <li
                key={row.instrument_id}
                className="rounded-md border border-border/50 px-2.5 py-1 text-sm text-muted-foreground"
              >
                <span className="font-medium text-foreground">
                  {row.symbol}
                </span>
                {row.failed_filters.length > 0
                  ? ` — ${row.failed_filters.join(", ")}`
                  : null}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </div>
  );
}
