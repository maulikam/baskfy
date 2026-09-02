import type { Metadata } from "next";

import { Answer, Mark } from "@/components/shell/answer";
import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { formatTradeDate } from "@/lib/format";
import { fetchPositions, type SwingPlan, type SwingPosition } from "@/lib/swing/fetch";
import { PAGES } from "@/lib/vocabulary";

/**
 * `/swing/positions` — the Positions tab of `docs/swing/05` §2, with the plan preview beside it.
 *
 * Two things a person needs before an open, and they belong on one page because they are read
 * together: **what is open and where its stop is**, and **what the rules want done with it at
 * tomorrow's open**.
 *
 * An unprotected position leads the page. `04` §6 and `03` §7 make a resting stop the one thing
 * the method insists on, and a row that is missing one is not a detail to notice halfway down a
 * table — so it is stated above everything else, in the same place whether or not there is one.
 *
 * Read-only. A line becomes an order in the desk console, on a click, and nowhere else.
 */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/swing/positions"].title,
  description: PAGES["/swing/positions"].blurb,
};

function money(value: number | null): string {
  return value === null ? "—" : value.toFixed(2);
}

/** How many R the position is showing, from the entry and the stop it was taken with. */
function showingR(position: SwingPosition): string {
  if (position.last_close === null) return "—";
  const oneR = position.entry_avg - position.initial_stop;
  if (oneR <= 0) return "—";
  return `${((position.last_close - position.entry_avg) / oneR).toFixed(2)}R`;
}

function PlanPreview({ plan }: { plan: SwingPlan }) {
  return (
    <section aria-label="Tomorrow's plan" className="space-y-3">
      <div className="space-y-1">
        <h2 className="text-lg font-medium">Tomorrow&rsquo;s plan</h2>
        <p className="max-w-[70ch] text-sm text-muted-foreground">
          Built {formatTradeDate(plan.as_of)} from the close. Exits first, because the money they
          free is the money the entries spend. Confirm a line in the desk console; nothing here
          sends anything.
        </p>
      </div>

      {plan.lines.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No lines. Either nothing qualified or the tape said no — the refusals below say which.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[48rem] border-collapse text-sm">
            <thead>
              <tr className="border-b border-border/70 text-left text-xs uppercase tracking-wide text-muted-foreground">
                <th className="py-2 pr-3 font-medium">Do</th>
                <th className="py-2 pr-3 font-medium">Stock</th>
                <th className="py-2 pr-3 font-medium">Qty</th>
                <th className="py-2 pr-3 font-medium">Trigger</th>
                <th className="py-2 pr-3 font-medium">Stop</th>
                <th className="py-2 pr-3 font-medium">Risk</th>
                <th className="py-2 pr-3 font-medium">Why</th>
              </tr>
            </thead>
            <tbody>
              {plan.lines.map((line) => (
                <tr key={line.id} className="border-b border-border/40">
                  <td className="py-2 pr-3 font-medium">{line.kind.replaceAll("_", " ")}</td>
                  <td className="py-2 pr-3">{line.symbol}</td>
                  <td className="py-2 pr-3 tabular-nums">{line.quantity || "—"}</td>
                  <td className="py-2 pr-3 tabular-nums">{money(line.trigger)}</td>
                  <td className="py-2 pr-3 tabular-nums">{money(line.stop)}</td>
                  <td className="py-2 pr-3 tabular-nums">
                    {line.risk_inr ? `₹${line.risk_inr.toFixed(0)}` : "—"}
                  </td>
                  <td className="py-2 pr-3 text-xs text-muted-foreground">{line.note ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="space-y-2">
        <h3 className="text-sm font-medium uppercase tracking-wide text-muted-foreground">
          Refused, and why
        </h3>
        {plan.skips.length === 0 ? (
          <p className="text-sm text-muted-foreground">Nothing was refused.</p>
        ) : (
          <ul className="space-y-1 text-sm">
            {plan.skips.map((skip) => (
              <li key={`${skip.symbol}-${skip.reason}`}>
                <span className="font-medium">{skip.symbol}</span>{" "}
                <span className="text-muted-foreground">
                  {skip.reason.replaceAll("_", " ").toLowerCase()}
                  {skip.detail ? ` — ${skip.detail}` : ""}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}

export default async function SwingPositionsPage() {
  const book = await fetchPositions();
  const rows = book?.data ?? [];
  const open = rows.filter((row) => row.state !== "CLOSED");
  const closed = rows.filter((row) => row.state === "CLOSED");
  const naked = open.filter((row) => row.naked);

  return (
    <div className="space-y-8">
      <PageHeader
        title={PAGES["/swing/positions"].title}
        blurb={PAGES["/swing/positions"].blurb}
        meta={
          <span className="text-sm text-muted-foreground">
            {open.length} open · {closed.length} closed
          </span>
        }
      />
      <SectionTabs section="swing" />

      <Answer
        footnote={
          "Every position here is labelled simulated until execution is enabled on the server. " +
          "Nothing on this page can place an order."
        }
      >
        {naked.length > 0 ? (
          <>
            <Mark>{naked.map((row) => row.symbol).join(", ")}</Mark>{" "}
            {naked.length === 1 ? "has" : "have"} no resting stop. Arm one before the open — a
            position without a stop is the one state this method does not allow.
          </>
        ) : open.length === 0 ? (
          <>Nothing is open.</>
        ) : (
          <>Every open position has a resting stop.</>
        )}
      </Answer>

      {book?.plan ? <PlanPreview plan={book.plan} /> : null}

      <section aria-label="Open positions" className="space-y-3">
        <h2 className="text-lg font-medium">Open</h2>
        {open.length === 0 ? (
          <p className="text-sm text-muted-foreground">Nothing is open.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[52rem] border-collapse text-sm">
              <thead>
                <tr className="border-b border-border/70 text-left text-xs uppercase tracking-wide text-muted-foreground">
                  <th className="py-2 pr-3 font-medium">Stock</th>
                  <th className="py-2 pr-3 font-medium">Entered</th>
                  <th className="py-2 pr-3 font-medium">At</th>
                  <th className="py-2 pr-3 font-medium">Open qty</th>
                  <th className="py-2 pr-3 font-medium">Stop</th>
                  <th className="py-2 pr-3 font-medium">Showing</th>
                  <th className="py-2 pr-3 font-medium">Trail</th>
                  <th className="py-2 pr-3 font-medium">Stop armed</th>
                </tr>
              </thead>
              <tbody>
                {open.map((row) => (
                  <tr key={row.id} className="border-b border-border/40">
                    <td className="py-2 pr-3">
                      <span className="font-medium">{row.symbol}</span>
                      {row.simulated ? (
                        <span className="ml-2 text-xs text-muted-foreground">simulated</span>
                      ) : null}
                    </td>
                    <td className="py-2 pr-3 tabular-nums">{formatTradeDate(row.entry_date)}</td>
                    <td className="py-2 pr-3 tabular-nums">{money(row.entry_avg)}</td>
                    <td className="py-2 pr-3 tabular-nums">
                      {row.quantity_open}
                      {row.partial_done ? (
                        <span className="ml-1 text-xs text-muted-foreground">part sold</span>
                      ) : null}
                    </td>
                    <td className="py-2 pr-3 tabular-nums">{money(row.stop)}</td>
                    <td className="py-2 pr-3 tabular-nums">{showingR(row)}</td>
                    <td className="py-2 pr-3">{row.trail}</td>
                    <td className="py-2 pr-3">
                      {row.gtt_id ? (
                        <span className="text-xs text-muted-foreground">{row.gtt_id}</span>
                      ) : (
                        <span className="text-xs font-medium text-red-600">none</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {closed.length > 0 ? (
        <section aria-label="Closed positions" className="space-y-3">
          <h2 className="text-lg font-medium">Closed</h2>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[42rem] border-collapse text-sm">
              <thead>
                <tr className="border-b border-border/70 text-left text-xs uppercase tracking-wide text-muted-foreground">
                  <th className="py-2 pr-3 font-medium">Stock</th>
                  <th className="py-2 pr-3 font-medium">Entered</th>
                  <th className="py-2 pr-3 font-medium">Closed</th>
                  <th className="py-2 pr-3 font-medium">R</th>
                  <th className="py-2 pr-3 font-medium">Why</th>
                </tr>
              </thead>
              <tbody>
                {closed.map((row) => (
                  <tr key={row.id} className="border-b border-border/40">
                    <td className="py-2 pr-3">
                      <span className="font-medium">{row.symbol}</span>
                      {row.simulated ? (
                        <span className="ml-2 text-xs text-muted-foreground">simulated</span>
                      ) : null}
                    </td>
                    <td className="py-2 pr-3 tabular-nums">{formatTradeDate(row.entry_date)}</td>
                    <td className="py-2 pr-3 tabular-nums">
                      {row.closed_on ? formatTradeDate(row.closed_on) : "—"}
                    </td>
                    <td className="py-2 pr-3 tabular-nums">
                      {row.r_multiple === null ? "—" : `${row.r_multiple.toFixed(2)}R`}
                    </td>
                    <td className="py-2 pr-3 text-xs text-muted-foreground">
                      {row.close_reason?.replaceAll("_", " ").toLowerCase() ?? "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}
    </div>
  );
}
