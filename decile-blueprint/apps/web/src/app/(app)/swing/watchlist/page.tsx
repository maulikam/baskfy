import type { Metadata } from "next";

import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { formatTradeDate } from "@/lib/format";
import { fetchWatchlist, type SwingWatchRow } from "@/lib/swing/fetch";
import { PAGES } from "@/lib/vocabulary";

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
 * Read-only here. Adding, annotating and dismissing go through the API (they move no money,
 * `docs/swing/02` Track A); this page renders the list.
 */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/swing/watchlist"].title,
  description: PAGES["/swing/watchlist"].blurb,
};

function money(value: number | null): string {
  return value === null ? "—" : value.toFixed(2);
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

export default async function SwingWatchlistPage() {
  const watchlist = await fetchWatchlist();
  const rows = [...(watchlist?.data ?? [])].sort(byProximity);

  return (
    <div className="space-y-8">
      <PageHeader
        title={PAGES["/swing/watchlist"].title}
        blurb={PAGES["/swing/watchlist"].blurb}
        meta={
          <span className="text-sm text-muted-foreground">
            {rows.length} {rows.length === 1 ? "name" : "names"}
          </span>
        }
      />
      <SectionTabs section="swing" />

      {rows.length === 0 ? (
        <p className="max-w-[70ch] text-sm text-muted-foreground">
          Nothing is being watched. The evening job puts every flag that scores 60 or better and
          every episodic pivot on this list; before it has run, or on an evening when nothing
          qualified, the list is empty and that is a fact about the market rather than about the
          system.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[52rem] border-collapse text-sm">
            <thead>
              <tr className="border-b border-border/70 text-left text-xs uppercase tracking-wide text-muted-foreground">
                <th className="py-2 pr-3 font-medium">Stock</th>
                <th className="py-2 pr-3 font-medium">Setup</th>
                <th className="py-2 pr-3 font-medium">Last</th>
                <th className="py-2 pr-3 font-medium">Trigger</th>
                <th className="py-2 pr-3 font-medium">Distance</th>
                <th className="py-2 pr-3 font-medium">Stop</th>
                <th className="py-2 pr-3 font-medium">Added</th>
                <th className="py-2 pr-3 font-medium">Expires</th>
                <th className="py-2 pr-3 font-medium">Catalyst</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id} className="border-b border-border/40">
                  <td className="py-2 pr-3">
                    <span className="font-medium">{row.symbol}</span>
                    <span className="ml-2 text-xs text-muted-foreground">{row.name}</span>
                    {row.source === "MANUAL" ? (
                      <span
                        className="ml-2 text-xs text-muted-foreground"
                        title="Added by hand. It has no expiry — the detectors did not put it here and will not take it away."
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
                  <td className="py-2 pr-3 tabular-nums">{formatTradeDate(row.added_on)}</td>
                  <td className="py-2 pr-3 tabular-nums">
                    {row.expires_on ? formatTradeDate(row.expires_on) : "—"}
                  </td>
                  <td className="py-2 pr-3 text-xs text-muted-foreground">
                    {row.catalyst ?? "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <p className="max-w-[70ch] text-sm text-muted-foreground">
        A flag stays here for ten sessions, an episodic pivot for three. When one expires it is
        marked expired rather than deleted — the record of what was watched is the record of what
        was passed over. Nothing on this page can place an order.
      </p>
    </div>
  );
}
