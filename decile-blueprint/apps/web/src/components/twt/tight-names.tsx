import { formatTradeDate } from "@/lib/format";
import { ENTRY_TODAY, WATCH_ONLY, pointInTimeLine } from "@/lib/twt/copy";
import type { TightNameView } from "@/lib/twt/view";
import { cn } from "@/lib/utils";

import { Disclosure } from "./disclosure";
import { FigureValue } from "./figure";

/**
 * `docs/twt/05` §1.2 — the names whose last three weekly closes sit within about 3% of each other.
 *
 * **The rejects are shown, greyed, with their reason.** `05` §1.2: "A screen that hides what it
 * rejected cannot be audited by the person whose money it is." They are inside a disclosure that
 * carries its own count rather than expanded below the table, which is the other half of the same
 * lesson: an explanation that is always open is an explanation nobody reads.
 *
 * **The point-in-time sentence is not decoration.** Chartink's own export of this screen evaluates
 * the week as a completed candle, so it knows Friday's close on Monday; a live run at the close
 * cannot, and therefore names fewer stocks on some days. That difference is the single most
 * likely question anybody will ask about this screen, and the answer belongs under the table
 * rather than in a document nobody opens.
 */
export function TightNames({
  rows,
  session,
}: {
  rows: readonly TightNameView[];
  session: string | null;
}) {
  const entries = rows.filter((row) => row.entry !== "WATCH_ONLY");
  const watchOnly = rows.filter((row) => row.entry === "WATCH_ONLY");

  return (
    <section aria-labelledby="twt-tight-heading" className="space-y-3">
      <h2
        id="twt-tight-heading"
        className="text-sm font-medium uppercase tracking-wide text-muted-foreground"
      >
        Quiet for three weeks
      </h2>

      {entries.length === 0 ? (
        /*
         * TWO DIFFERENT ABSENCES, AND THE PAGE USED TO STATE THE WRONG ONE (12 Sep 2026).
         *
         * "No name held the pattern on this session" asserts that a session WAS read and that
         * nothing matched. On a database where the detector has never run there is no session at
         * all, and the sentence is simply false — which is what it said on the box the day TWT was
         * first deployed, two panels below a gate card correctly reading "Not read yet".
         *
         * `session` is the same null the gate card branches on, and it was already a prop here.
         */
        session === null ? (
          <p className="text-sm text-muted-foreground" data-testid="twt-tight-unread">
            No session has been read for this strategy yet, so there is nothing to say about which
            names were quiet. The scan runs after the close on a trading day; until one has run,
            this is empty because nothing has looked, not because nothing qualified.
          </p>
        ) : (
          <p className="text-sm text-muted-foreground" data-testid="twt-tight-empty">
            No name held the pattern on this session. That is an ordinary result: the pattern needs
            three weekly closes within about 3% of one another, and most weeks most names move more
            than that.
          </p>
        )
      ) : (
        <NameTable rows={entries} />
      )}

      {session ? (
        <p
          className="max-w-[80ch] text-sm text-muted-foreground"
          data-testid="twt-point-in-time"
        >
          {pointInTimeLine(formatTradeDate(session))}
        </p>
      ) : null}

      <Disclosure summary="The method note" testId="twt-method-note">
        <p>
          The screen reads the closed daily price. The current week counts as a week in progress:
          today&rsquo;s close stands in for this week&rsquo;s close, beside the last close of each
          of the two weeks before it. Chartink&rsquo;s historical export instead uses each
          week&rsquo;s final close on every day of that week, which is a price that had not
          happened yet on the Monday it is applied to.
        </p>
        <p>
          Both readings were measured against Chartink&rsquo;s own export. The one used here is
          what a person running the screen at the close would actually have seen, which is the
          only reading a live entry can be taken from.
        </p>
      </Disclosure>

      {watchOnly.length > 0 ? (
        <Disclosure
          summary="Held the pattern but was not entered"
          count={watchOnly.length}
          testId="twt-watch-only"
        >
          <p>
            Each of these met every rule of the pattern and was still passed over, for the reason
            beside it. They are here because a screen that shows only what it accepted cannot be
            checked.
          </p>
          <ul className="space-y-1.5">
            {watchOnly.map((row) => (
              <li key={row.instrumentId} data-testid="twt-watch-only-row">
                <span className="font-medium text-foreground">{row.symbol}</span>{" "}
                <span className="rounded bg-muted px-1.5 py-0.5 text-xs text-muted-foreground">
                  {WATCH_ONLY}
                </span>
                {" — "}
                {row.watchReason}
              </li>
            ))}
          </ul>
        </Disclosure>
      ) : null}
    </section>
  );
}

function NameTable({ rows }: { rows: readonly TightNameView[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[56rem] border-collapse text-sm">
        <caption className="sr-only">
          Names holding the three-weeks-tight pattern, most heavily traded first
        </caption>
        <thead>
          <tr className="border-b border-border/70 text-left text-xs uppercase tracking-wide text-muted-foreground">
            <th scope="col" className="py-2 pr-3 font-medium">
              Stock
            </th>
            <th scope="col" className="py-2 pr-3 font-medium">
              Close
            </th>
            <th scope="col" className="py-2 pr-3 font-medium">
              Three weekly closes
            </th>
            <th scope="col" className="py-2 pr-3 font-medium">
              Spread across them
            </th>
            <th scope="col" className="py-2 pr-3 font-medium">
              Above its three-month low
            </th>
            <th scope="col" className="py-2 pr-3 font-medium">
              Sessions quiet
            </th>
            <th scope="col" className="py-2 pr-3 font-medium">
              Entry today
            </th>
            <th scope="col" className="py-2 pr-3 font-medium">
              Traded a day
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={row.instrumentId}
              className="border-b border-border/40"
              data-testid="twt-tight-row"
            >
              <td className="py-2 pr-3">
                <span className="font-medium">{row.symbol}</span>
                {row.lockedUpperCircuit ? (
                  <span
                    className="ml-1.5 text-warning"
                    title="It closed locked at its upper circuit, so there was no price to buy at"
                    data-testid="twt-locked"
                  >
                    &#9888;
                  </span>
                ) : null}
                <span className="block text-xs text-muted-foreground">{row.name}</span>
              </td>
              <td className="py-2 pr-3">
                <FigureValue figure={row.close} />
              </td>
              <td className="py-2 pr-3">
                <span className="flex flex-wrap gap-x-2">
                  {row.weekCloses.map((close, index) => (
                    <FigureValue key={index} figure={close} />
                  ))}
                </span>
              </td>
              <td className="py-2 pr-3">
                <FigureValue figure={row.weekRange} />
              </td>
              <td className="py-2 pr-3">
                <FigureValue figure={row.aboveMonthLow} />
              </td>
              <td className="py-2 pr-3">
                <FigureValue figure={row.sessionsInState} />
              </td>
              <td className="py-2 pr-3">
                {row.entry === "ENTRY" ? (
                  <span
                    className={cn(
                      "rounded-md bg-accent-muted px-1.5 py-0.5 text-xs font-medium text-accent",
                    )}
                    data-testid="twt-entry-badge"
                  >
                    {ENTRY_TODAY}
                  </span>
                ) : (
                  <span className="text-xs text-muted-foreground">Still quiet</span>
                )}
              </td>
              <td className="py-2 pr-3">
                <FigureValue figure={row.turnoverCrore} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
