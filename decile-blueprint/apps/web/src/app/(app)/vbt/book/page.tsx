import type { Metadata } from "next";

import { Answer, Mark } from "@/components/shell/answer";
import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { formatTradeDate } from "@/lib/format";
import { fetchBook } from "@/lib/vbt/fetch";
import { PAGES } from "@/lib/vocabulary";

import { sessionsLine } from "../copy";

/**
 * `/vbt/book` — the Book tab of `docs/vbt/05` §2.
 *
 * Three tables and one line. The working orders come first because they are the only thing on
 * this page with a deadline: a limit is cancelled at the close of its third session (`04` §7.2),
 * and a row that says "cancels tonight" is the one fact here somebody can still act on.
 *
 * The fill-rate line under them is `04` §7.3's honest early warning — this book's fills against
 * the study's modelled 83.8% — and it is on the page from the first fill, because the gap between
 * those two numbers is the single most likely place the live result parts company with the study.
 *
 * **Read-only.** Arming a stop and cancelling a limit are order-shaped and belong to the desk.
 */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/vbt/book"].title,
  description: PAGES["/vbt/book"].blurb,
};

function money(value: number | null): string {
  return value === null ? "—" : value.toFixed(2);
}

function rupees(value: number | null): string {
  return value === null
    ? "—"
    : `₹${value.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
}

function percent(value: number | null): string {
  return value === null ? "—" : `${value.toFixed(1)}%`;
}

function SimulatedTag({ simulated }: { simulated: boolean }) {
  if (!simulated) return null;
  return (
    <span
      className="ml-1.5 rounded border border-border px-1 py-0.5 text-[10px] uppercase tracking-wide text-muted-foreground"
      data-testid="simulated-tag"
    >
      simulated
    </span>
  );
}

export default async function VbtBookPage() {
  const book = await fetchBook();
  const working = book?.working ?? [];
  const open = book?.open_positions ?? [];
  const closed = book?.closed_positions ?? [];
  const fill = book?.fill_rate ?? null;
  const naked = open.filter((row) => row.naked).length;
  const expiring = working.filter((row) => row.expires_tonight).length;

  return (
    <div className="space-y-8">
      <PageHeader
        title={PAGES["/vbt/book"].title}
        blurb={PAGES["/vbt/book"].blurb}
      />
      <SectionTabs section="vbt" />

      <Answer
        footnote={
          "Every row here is what this strategy's own tables say. Arming a stop and cancelling a " +
          "limit are order-shaped and happen in the desk console, on a click — never here."
        }
      >
        {open.length === 0 && working.length === 0 ? (
          <>Nothing is held and no limits are working.</>
        ) : (
          <>
            <Mark>{open.length}</Mark>{" "}
            {open.length === 1 ? "position" : "positions"} open and{" "}
            <Mark>{working.length}</Mark>{" "}
            {working.length === 1 ? "limit" : "limits"} working
            {expiring > 0 ? (
              <>
                , <Mark>{expiring}</Mark> of which{" "}
                {expiring === 1 ? "cancels" : "cancel"} tonight
              </>
            ) : null}
            {naked > 0 ? (
              <>
                .{" "}
                <span data-testid="naked-warning">
                  <Mark>{naked} without a resting stop</Mark>
                </span>{" "}
                — the one state the method forbids
              </>
            ) : null}
            .
          </>
        )}
      </Answer>

      {fill ? (
        <p
          className="max-w-[80ch] text-sm text-muted-foreground"
          data-testid="fill-rate-line"
        >
          {fill.resolved === 0 ? (
            <>
              No limit has resolved yet, so this book has no fill rate. The
              study modelled{" "}
              <span className="tabular-nums text-foreground">
                {fill.modelled_pct.toFixed(1)}%
              </span>
              .
            </>
          ) : (
            <>
              This book&rsquo;s limits filled{" "}
              <span className="tabular-nums text-foreground">
                {fill.filled}
              </span>{" "}
              of{" "}
              <span className="tabular-nums text-foreground">
                {fill.resolved}
              </span>{" "}
              times within three sessions ({percent(fill.rate_pct)}). The study
              modelled{" "}
              <span className="tabular-nums text-foreground">
                {fill.modelled_pct.toFixed(1)}%
              </span>
              .
            </>
          )}
        </p>
      ) : null}

      <section aria-label="Working orders" className="space-y-2">
        <h2 className="text-sm font-medium uppercase tracking-wide text-muted-foreground">
          Limits working
        </h2>
        {working.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No limits are working.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[60rem] border-collapse text-sm">
              <thead>
                <tr className="border-b border-border/70 text-left text-xs uppercase tracking-wide text-muted-foreground">
                  <th className="py-2 pr-3 font-medium">Stock</th>
                  <th className="py-2 pr-3 font-medium">Limit</th>
                  <th className="py-2 pr-3 font-medium">Quantity</th>
                  <th className="py-2 pr-3 font-medium">Value</th>
                  <th className="py-2 pr-3 font-medium">Stop it will get</th>
                  <th className="py-2 pr-3 font-medium">Signal</th>
                  <th className="py-2 pr-3 font-medium">Sessions</th>
                  <th className="py-2 pr-3 font-medium">State</th>
                  <th className="py-2 pr-3 font-medium">Broker id</th>
                </tr>
              </thead>
              <tbody>
                {working.map((row) => (
                  <tr
                    key={row.id}
                    className="border-b border-border/40"
                    data-expiring={row.expires_tonight ? "true" : undefined}
                  >
                    <td className="py-2 pr-3">
                      <span className="font-medium">{row.symbol}</span>
                      <SimulatedTag simulated={row.simulated} />
                    </td>
                    <td className="py-2 pr-3 tabular-nums">
                      {money(row.limit_price)}
                    </td>
                    <td className="py-2 pr-3 tabular-nums">{row.quantity}</td>
                    <td className="py-2 pr-3 tabular-nums">
                      {rupees(row.value_inr)}
                    </td>
                    <td className="py-2 pr-3 tabular-nums">
                      {money(row.stop_price)}
                    </td>
                    <td className="py-2 pr-3">
                      {formatTradeDate(row.signal_date)}
                    </td>
                    <td
                      className={
                        row.expires_tonight
                          ? "py-2 pr-3 font-medium text-warning"
                          : "py-2 pr-3 text-muted-foreground"
                      }
                    >
                      {sessionsLine(
                        row.sessions_worked,
                        row.sessions_allowed,
                        row.expires_tonight,
                      )}
                    </td>
                    <td className="py-2 pr-3 text-muted-foreground">
                      {row.state}
                    </td>
                    <td className="py-2 pr-3 text-muted-foreground">
                      {row.broker_order_id ?? "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section aria-label="Open positions" className="space-y-2">
        <h2 className="text-sm font-medium uppercase tracking-wide text-muted-foreground">
          Open
        </h2>
        {open.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            Nothing is held.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[64rem] border-collapse text-sm">
              <thead>
                <tr className="border-b border-border/70 text-left text-xs uppercase tracking-wide text-muted-foreground">
                  <th className="py-2 pr-3 font-medium">Stock</th>
                  <th className="py-2 pr-3 font-medium">Entered</th>
                  <th className="py-2 pr-3 font-medium">Average</th>
                  <th className="py-2 pr-3 font-medium">Quantity</th>
                  <th className="py-2 pr-3 font-medium">Last</th>
                  <th className="py-2 pr-3 font-medium">Stop</th>
                  <th className="py-2 pr-3 font-medium">GTT</th>
                  <th className="py-2 pr-3 font-medium">Return</th>
                  <th className="py-2 pr-3 font-medium">R</th>
                  <th className="py-2 pr-3 font-medium">Held</th>
                  <th className="py-2 pr-3 font-medium">Queued</th>
                </tr>
              </thead>
              <tbody>
                {open.map((row) => (
                  <tr key={row.id} className="border-b border-border/40">
                    <td className="py-2 pr-3">
                      <span className="font-medium">{row.symbol}</span>
                      <SimulatedTag simulated={row.simulated} />
                    </td>
                    <td className="py-2 pr-3">
                      {formatTradeDate(row.entry_date)}
                    </td>
                    <td className="py-2 pr-3 tabular-nums">
                      {money(row.entry_avg)}
                    </td>
                    <td className="py-2 pr-3 tabular-nums">
                      {row.quantity_open}
                    </td>
                    <td className="py-2 pr-3 tabular-nums">
                      {money(row.last_close)}
                    </td>
                    <td className="py-2 pr-3 tabular-nums">
                      {money(row.stop_price)}
                    </td>
                    <td className="py-2 pr-3">
                      {row.naked ? (
                        <span
                          className="font-medium text-rose-700"
                          data-testid="naked-cell"
                        >
                          naked
                        </span>
                      ) : (
                        <span className="text-muted-foreground">armed</span>
                      )}
                    </td>
                    <td className="py-2 pr-3 tabular-nums">
                      {percent(row.return_pct)}
                    </td>
                    <td className="py-2 pr-3 tabular-nums">
                      {row.r_multiple === null
                        ? "—"
                        : `${row.r_multiple.toFixed(2)}R`}
                    </td>
                    <td className="py-2 pr-3 tabular-nums">
                      {row.sessions_held ?? "—"}
                    </td>
                    <td className="py-2 pr-3 text-muted-foreground">
                      {row.exit_queued_for
                        ? `sells at ${formatTradeDate(row.exit_queued_for)}'s open`
                        : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {closed.length > 0 ? (
        <section aria-label="Closed positions" className="space-y-2">
          <h2 className="text-sm font-medium uppercase tracking-wide text-muted-foreground">
            Closed
          </h2>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[56rem] border-collapse text-sm">
              <thead>
                <tr className="border-b border-border/70 text-left text-xs uppercase tracking-wide text-muted-foreground">
                  <th className="py-2 pr-3 font-medium">Stock</th>
                  <th className="py-2 pr-3 font-medium">Entered</th>
                  <th className="py-2 pr-3 font-medium">Closed</th>
                  <th className="py-2 pr-3 font-medium">In</th>
                  <th className="py-2 pr-3 font-medium">Out</th>
                  <th className="py-2 pr-3 font-medium">Return</th>
                  <th className="py-2 pr-3 font-medium">R</th>
                  <th className="py-2 pr-3 font-medium">Why</th>
                </tr>
              </thead>
              <tbody>
                {closed.map((row) => (
                  <tr key={row.id} className="border-b border-border/40">
                    <td className="py-2 pr-3">
                      <span className="font-medium">{row.symbol}</span>
                      <SimulatedTag simulated={row.simulated} />
                    </td>
                    <td className="py-2 pr-3">
                      {formatTradeDate(row.entry_date)}
                    </td>
                    <td className="py-2 pr-3">
                      {row.closed_on ? formatTradeDate(row.closed_on) : "—"}
                    </td>
                    <td className="py-2 pr-3 tabular-nums">
                      {money(row.entry_avg)}
                    </td>
                    <td className="py-2 pr-3 tabular-nums">
                      {money(row.exit_avg)}
                    </td>
                    <td className="py-2 pr-3 tabular-nums">
                      {percent(row.return_pct)}
                    </td>
                    <td className="py-2 pr-3 tabular-nums">
                      {row.r_multiple === null
                        ? "—"
                        : `${row.r_multiple.toFixed(2)}R`}
                    </td>
                    <td className="py-2 pr-3 text-muted-foreground">
                      {row.close_reason?.replaceAll("_", " ").toLowerCase() ??
                        "—"}
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
