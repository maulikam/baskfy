import type { Metadata } from "next";

import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { CatalystLink } from "@/components/swing/catalyst-link";
import { formatTradeDate } from "@/lib/format";
import {
  fetchSignals,
  fetchWatchlist,
  type SwingSignal,
  type SwingWatchRow,
} from "@/lib/swing/fetch";
import { PAGES } from "@/lib/vocabulary";

import { AnnotateForm, RowActionForm, WatchAddForm } from "../_components/forms";
import { watchAdd, watchAnnotate, watchDismiss, watchReconfirm } from "../actions";
import { signalSentence, stopShareOfAdr } from "./copy";

/**
 * `/swing/watchlist` — the Watchlist tab of `docs/swing/05` §2.
 *
 * The method's memory between the scan and the morning. `docs/swing/01` §8: the weekend produces
 * "a watchlist of a few dozen forming flags, the levels that would trigger next week", and the
 * first hour of a session is spent watching *those* levels rather than looking for new ones.
 *
 * So the column that matters is **distance to trigger** — how close each name is to going — and
 * the rows are ordered by it rather than by score. A list sorted by how good a setup looks tells
 * you what to admire; a list sorted by how close it is tells you what to watch.
 *
 * The writes here are the ones `02` Track A allows because they move no money: a name added by
 * hand, a note, a MANUAL row re-confirmed (STANDING-ANSWERS A14), a row dismissed. Under each
 * row, what the monitor saw last session (`sw_signal`, SW14) — a record, never an instruction.
 */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/swing/watchlist"].title,
  description: PAGES["/swing/watchlist"].blurb,
};

/** STANDING-ANSWERS A14's funnel: the evening watches this many flags, and this many are in focus. */
const AUTO_WATCH_FLAGS = 20;
const FOCUS_FLAGS = 5;

function money(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : value.toFixed(2);
}

function distance(row: SwingWatchRow): string {
  if (row.distance_to_trigger_pct === null) return "—";
  const value = row.distance_to_trigger_pct;
  return value < 0 ? `${Math.abs(value).toFixed(2)}% above` : `${value.toFixed(2)}% away`;
}

/** Nearest to its trigger first; a row with no level or no price sorts last. */
function byProximity(a: SwingWatchRow, b: SwingWatchRow): number {
  const left = a.distance_to_trigger_pct;
  const right = b.distance_to_trigger_pct;
  if (left === null && right === null) return a.symbol.localeCompare(b.symbol);
  if (left === null) return 1;
  if (right === null) return -1;
  return left - right;
}

/** Focus rows first, then by proximity — the order the desk page and the push use (A14). */
function byFocusThenProximity(a: SwingWatchRow, b: SwingWatchRow): number {
  if (Boolean(a.focus) !== Boolean(b.focus)) return a.focus ? -1 : 1;
  return byProximity(a, b);
}

function History({ row }: { row: SwingWatchRow }) {
  const steps = [`added ${formatTradeDate(row.added_on)}`];
  if (row.reconfirmed_on) steps.push(`re-confirmed ${formatTradeDate(row.reconfirmed_on)}`);
  steps.push(row.expires_on ? `expires ${formatTradeDate(row.expires_on)}` : "no expiry");
  return <span className="text-xs text-muted-foreground">{steps.join(" · ")}</span>;
}

export default async function SwingWatchlistPage() {
  const [watchlist, signals] = await Promise.all([fetchWatchlist(), fetchSignals()]);
  const rows = [...(watchlist?.data ?? [])].sort(byFocusThenProximity);
  const focus = rows.filter((row) => row.focus).length;
  const detectorFlags = rows.filter(
    (row) => row.source === "DETECTOR" && row.setup === "FLAG",
  ).length;
  const pivots = rows.filter((row) => row.setup === "EP").length;

  const fired = new Map<number, SwingSignal[]>();
  for (const signal of signals?.data ?? []) {
    const list = fired.get(signal.instrument_id) ?? [];
    list.push(signal);
    fired.set(signal.instrument_id, list);
  }
  const columns = 11;

  return (
    <div className="space-y-8">
      <PageHeader
        title={PAGES["/swing/watchlist"].title}
        blurb={PAGES["/swing/watchlist"].blurb}
        meta={
          <span className="text-sm text-muted-foreground" data-testid="funnel">
            {rows.length} {rows.length === 1 ? "name" : "names"} ·{" "}
            <span title="Today's focus: the top five flags by score and every episodic pivot. Pushed at the trigger and first on the desk page.">
              {focus} in focus of {FOCUS_FLAGS} + every pivot
            </span>{" "}
            · {detectorFlags} of the {AUTO_WATCH_FLAGS} flags the evening watches · {pivots}{" "}
            {pivots === 1 ? "pivot" : "pivots"}
          </span>
        }
      />
      <SectionTabs section="swing" />

      <section aria-label="Add a name by hand" className="space-y-2">
        <h2 className="text-sm font-medium uppercase tracking-wide text-muted-foreground">
          Add by hand
        </h2>
        <WatchAddForm action={watchAdd} />
        <p className="max-w-[70ch] text-xs text-muted-foreground">
          A name you add stays ten sessions, then expires unless you say you are still watching
          it — a two-week-old typed pivot is stale, and hand-typed levels are not refreshed
          before the open.
        </p>
      </section>

      {rows.length === 0 ? (
        <p className="max-w-[70ch] text-sm text-muted-foreground">
          Nothing is being watched. The evening job puts the top {AUTO_WATCH_FLAGS} flags by
          score and every episodic pivot on this list; before it has run, or on an evening when
          nothing qualified, the list is empty and that is a fact about the market rather than
          about the system.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[72rem] border-collapse text-sm">
            <thead>
              <tr className="border-b border-border/70 text-left text-xs uppercase tracking-wide text-muted-foreground">
                <th className="py-2 pr-3 font-medium">Stock</th>
                <th className="py-2 pr-3 font-medium">Setup</th>
                <th className="py-2 pr-3 font-medium">Last</th>
                <th className="py-2 pr-3 font-medium">Trigger</th>
                <th className="py-2 pr-3 font-medium">Distance</th>
                <th className="py-2 pr-3 font-medium">Stop</th>
                <th className="py-2 pr-3 font-medium">Stop / ADR</th>
                <th className="py-2 pr-3 font-medium">Score</th>
                <th className="py-2 pr-3 font-medium">On the list</th>
                <th className="py-2 pr-3 font-medium">Catalyst</th>
                <th className="py-2 pr-3 font-medium">Actions</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const share = stopShareOfAdr(row);
                const willSkip = share !== null && share > 1;
                const seen = fired.get(row.instrument_id) ?? [];
                return [
                  <tr
                    key={row.id}
                    className={row.focus ? "bg-muted/40" : undefined}
                    data-focus={row.focus ? "true" : undefined}
                  >
                    <td className="py-2 pr-3">
                      {row.focus ? (
                        <span
                          className="mr-1.5 text-warning"
                          title="In today's focus — the top five flags by score and every episodic pivot."
                          aria-label="focus"
                        >
                          ★
                        </span>
                      ) : null}
                      <span className="font-medium">{row.symbol}</span>
                      <span className="ml-2 text-xs text-muted-foreground">{row.name}</span>
                      {row.source === "MANUAL" ? (
                        <span
                          className="ml-2 text-xs text-muted-foreground"
                          title="Added by hand. It expires after ten sessions unless you re-confirm it."
                        >
                          yours
                        </span>
                      ) : null}
                    </td>
                    <td className="py-2 pr-3">{row.setup}</td>
                    <td className="py-2 pr-3 tabular-nums">{money(row.last_close)}</td>
                    <td className="py-2 pr-3 tabular-nums font-medium">{money(row.trigger)}</td>
                    <td className="py-2 pr-3 tabular-nums">{distance(row)}</td>
                    <td className="py-2 pr-3 tabular-nums">{money(row.stop_ref)}</td>
                    <td className="py-2 pr-3 tabular-nums">
                      {share === null ? (
                        "—"
                      ) : (
                        <span className={willSkip ? "text-warning" : undefined}>
                          {share.toFixed(2)} ADR
                          {willSkip ? (
                            <span
                              className="ml-1.5 rounded border border-warning/50 px-1 text-[11px] uppercase tracking-wide"
                              title="The stop is wider than one day's average range, so the plan will skip this name rather than size it (STOP_TOO_WIDE)."
                            >
                              will skip
                            </span>
                          ) : null}
                        </span>
                      )}
                    </td>
                    <td className="py-2 pr-3 tabular-nums">
                      {row.score === null || row.score === undefined ? "—" : row.score.toFixed(0)}
                    </td>
                    <td className="py-2 pr-3">
                      <span className="block text-xs">
                        {row.source === "MANUAL" ? "by hand" : "detector"}
                      </span>
                      <History row={row} />
                    </td>
                    <td className="py-2 pr-3 text-xs text-muted-foreground">
                      <span className="block">{row.catalyst ?? "—"}</span>
                      <CatalystLink
                        feed={
                          row.catalyst_feed ??
                          (row.earnings_date
                            ? {
                                headline: null,
                                published_at: null,
                                url: null,
                                earnings_date: row.earnings_date,
                              }
                            : null)
                        }
                      />
                    </td>
                    <td className="py-2 pr-3">
                      <div className="flex flex-col gap-2">
                        <AnnotateForm
                          action={watchAnnotate}
                          id={row.id}
                          note={row.note}
                          catalyst={row.catalyst}
                        />
                        <div className="flex flex-wrap items-center gap-2">
                          {row.source === "MANUAL" ? (
                            <RowActionForm
                              action={watchReconfirm}
                              fields={{ id: row.id }}
                              label="Still watching"
                              pendingLabel="Re-confirming…"
                              title="Restart this row's ten-session clock. No level changes."
                            />
                          ) : null}
                          <RowActionForm
                            action={watchDismiss}
                            fields={{ id: row.id }}
                            label="Dismiss"
                            pendingLabel="Dismissing…"
                            title="Mark this row dismissed. It is kept, as the record of a no."
                            variant="ghost"
                          />
                        </div>
                      </div>
                    </td>
                  </tr>,
                  seen.length > 0 ? (
                    <tr key={`${row.id}-signals`} className="border-b border-border/40">
                      <td colSpan={columns} className="pb-2 pl-6 pr-3 text-xs text-muted-foreground">
                        <span className="font-medium text-foreground">
                          {signals?.session_date ? formatTradeDate(signals.session_date) : "Last session"}
                        </span>
                        {seen.map((signal) => (
                          <span key={signal.id} className="ml-3" data-testid="signal">
                            {signalSentence(signal)}
                          </span>
                        ))}
                      </td>
                    </tr>
                  ) : (
                    <tr key={`${row.id}-spacer`} className="border-b border-border/40">
                      <td colSpan={columns} className="p-0" />
                    </tr>
                  ),
                ];
              })}
            </tbody>
          </table>
        </div>
      )}

      <p className="max-w-[70ch] text-sm text-muted-foreground">
        The catalyst column is filled at 09:10 from NSE&apos;s corporate announcements for the
        names on this list — a headline that links to the exchange&apos;s own copy of the
        filing, and an earnings badge when a result meeting is on the calendar. Anything you
        type there stays; the feed only fills blanks. Nothing from a filing is reproduced here.
      </p>

      <p className="max-w-[70ch] text-sm text-muted-foreground">
        A flag stays here for ten sessions, an episodic pivot for three, a name you added until
        ten sessions after you last re-confirmed it. When one expires it is marked expired rather
        than deleted — the record of what was watched is the record of what was passed over. The
        line under a name is what the monitor saw last session; every verdict is kept, not only
        the triggers. Nothing on this page can place an order.
      </p>
    </div>
  );
}
