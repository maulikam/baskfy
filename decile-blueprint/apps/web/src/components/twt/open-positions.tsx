import { formatTradeDate } from "@/lib/format";
import { toneOf } from "@/lib/twt/numbers";
import type { PositionView } from "@/lib/twt/view";

import { FigureValue } from "./figure";

/**
 * `docs/twt/05` §1.3 — what is open, where its stop is, and **how far the price can fall before
 * that stop sells it**.
 *
 * THE DISTANCE COLUMN IS THE POINT OF THIS TABLE
 * ----------------------------------------------
 * `01` §8: the trail is the strategy and it is slow. A 20% give-back on a ₹2.5 lakh position is a
 * ₹50,000 open loss this strategy will sit through as a matter of routine — not as a failure, as
 * the mechanism working. `05` §1.3 puts the distance on every row for exactly that reason: the
 * person watching has to have agreed to that number in advance rather than discover it on the
 * afternoon it happens.
 *
 * A NAKED LINE IS A STATE, NOT A MISSING NUMBER
 * ---------------------------------------------
 * Shares open with nothing resting at the exchange is the one state the method forbids. The row
 * says so in words and in the negative tone, and the two figures that depend on a stop say the
 * same thing rather than showing a dash or — far worse — a zero distance, which reads as "the
 * stop is right at the price" when the truth is that there is no stop at all.
 */
export function OpenPositions({ rows }: { rows: readonly PositionView[] }) {
  return (
    <section aria-labelledby="twt-open-heading" className="space-y-3">
      <h2
        id="twt-open-heading"
        className="text-sm font-medium uppercase tracking-wide text-muted-foreground"
      >
        Open positions
      </h2>

      {rows.length === 0 ? (
        <p className="max-w-[80ch] text-sm text-muted-foreground" data-testid="twt-open-empty">
          Nothing is open in this strategy. It takes a position only when a name that has been
          quiet for three weeks signals an entry and the market breadth allows it, so an empty
          list here is an ordinary state rather than a sign that something did not run.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[64rem] border-collapse text-sm">
            <caption className="sr-only">
              Open positions, with the stop resting at the exchange and the distance to it
            </caption>
            <thead>
              <tr className="border-b border-border/70 text-left text-xs uppercase tracking-wide text-muted-foreground">
                <th scope="col" className="py-2 pr-3 font-medium">
                  Stock
                </th>
                <th scope="col" className="py-2 pr-3 font-medium">
                  Bought
                </th>
                <th scope="col" className="py-2 pr-3 font-medium">
                  Quantity
                </th>
                <th scope="col" className="py-2 pr-3 font-medium">
                  Highest since buying
                </th>
                <th scope="col" className="py-2 pr-3 font-medium">
                  Stop in force
                </th>
                <th scope="col" className="py-2 pr-3 font-medium">
                  Room before the stop
                </th>
                <th scope="col" className="py-2 pr-3 font-medium">
                  Gain so far
                </th>
                <th scope="col" className="py-2 pr-3 font-medium">
                  Held
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr
                  key={row.id}
                  className="border-b border-border/40 align-top"
                  data-testid="twt-position-row"
                >
                  <td className="py-2 pr-3">
                    <span className="font-medium">{row.symbol}</span>
                    <span className="block text-xs text-muted-foreground">{row.name}</span>
                    <span className="mt-1 flex flex-wrap gap-1">
                      {row.halfSize ? (
                        <span
                          className="rounded bg-muted px-1.5 py-0.5 text-xs text-muted-foreground"
                          data-testid="twt-half-size-badge"
                        >
                          Half size
                        </span>
                      ) : null}
                      {row.simulated ? (
                        <span
                          className="rounded bg-muted px-1.5 py-0.5 text-xs text-muted-foreground"
                          data-testid="twt-simulated-badge"
                        >
                          Practice only
                        </span>
                      ) : null}
                    </span>
                  </td>
                  <td className="py-2 pr-3">
                    <FigureValue figure={row.entryPrice} />
                    <span className="block text-xs text-muted-foreground">
                      {formatTradeDate(row.entryDate)}
                    </span>
                  </td>
                  <td className="py-2 pr-3">
                    <FigureValue figure={row.quantity} />
                  </td>
                  <td className="py-2 pr-3">
                    <FigureValue figure={row.highSince} />
                    {row.highSinceDate ? (
                      <span className="block text-xs text-muted-foreground">
                        set {formatTradeDate(row.highSinceDate)}
                      </span>
                    ) : null}
                  </td>
                  <td className="py-2 pr-3">
                    {row.naked ? (
                      <span
                        className="font-medium text-negative"
                        data-testid="twt-naked"
                      >
                        No stop resting
                      </span>
                    ) : (
                      <FigureValue figure={row.stopInForce} />
                    )}
                    {row.ratchetDue ? (
                      <span
                        className="block text-xs text-muted-foreground"
                        data-testid="twt-ratchet-due"
                      >
                        rises tomorrow: {row.ratchetDue.from} &rarr; {row.ratchetDue.to}
                      </span>
                    ) : null}
                  </td>
                  <td className="py-2 pr-3">
                    <FigureValue figure={row.distanceToTrigger} />
                  </td>
                  <td className="py-2 pr-3">
                    <FigureValue
                      figure={row.unrealised}
                      tone={toneOf(row.unrealisedPct.value)}
                    />
                    <span className="block text-xs">
                      <FigureValue
                        figure={row.unrealisedPct}
                        tone={toneOf(row.unrealisedPct.value)}
                      />
                    </span>
                  </td>
                  <td className="py-2 pr-3">
                    <FigureValue figure={row.hold} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {rows.some((row) => row.naked) ? (
        <p className="max-w-[80ch] text-sm text-negative" data-testid="twt-naked-note">
          A position with no stop resting at the exchange is not protected. Placing it is an
          action taken on the desk, and until it is, the fall shown for that position has no floor
          under it.
        </p>
      ) : null}
    </section>
  );
}
