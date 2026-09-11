import type { ReactNode } from "react";

import { formatTradeDate } from "@/lib/format";
import type { DeskLineView, DeskView } from "@/lib/twt/desk";
import type { PositionView } from "@/lib/twt/view";
import { cn } from "@/lib/utils";

import { FigureValue } from "../figure";
import { OpenPositions } from "../open-positions";

/**
 * The operator's page for one session's plan — `docs/twt/05` §2.
 *
 * WHY IT IS A COMPONENT HERE AND NOT A ROUTE
 * ------------------------------------------
 * The web app has no order path and does not gain one. That is the product's first
 * non-negotiable and `docs/twt/02` Track C §4 by name, and a Confirm button wired to anything in
 * `apps/web` would breach both. So this file is the page's **shape** — the section order, the
 * badge, the expiry rule, the 15:15 band — with the confirm control left as a slot the host
 * supplies. The web app supplies none, which is why there is no `/twt` route that renders this.
 * Recorded as DECISIONS-TW TW8.1. TW6 is what wires a real confirm behind it, in the desk
 * console, where a plan line becomes an order through the gateway and nowhere else.
 *
 * THE RULE MOST WORTH A FAILING TEST
 * ----------------------------------
 * **An expired plan's controls are absent, not disabled.** A greyed-out Confirm still looks like
 * a button that could work, so it invites a reload and a retry, and a person retrying an expired
 * plan is a person looking for a way to send it anyway. Nothing renders at all, under a banner
 * that says the plan expired and has to be rebuilt. `__tests__/desk-plan.test.tsx` asserts the
 * absence rather than a `disabled` attribute, because asserting `disabled` would pass on exactly
 * the implementation this rule forbids.
 */
export interface DeskPlanProps {
  view: DeskView;
  /**
   * The confirm control for one line, supplied by the host that can actually send it.
   *
   * It is called only for a line whose `confirmable` is true — never on an expired plan, never on
   * a line already sent. A host that ignores that and renders unconditionally cannot: the slot is
   * not invoked at all.
   */
  renderConfirm?: (line: DeskLineView) => ReactNode;
  /** The per-position control for putting a stop back on an unprotected line. Same contract. */
  renderRearm?: (position: PositionView) => ReactNode;
}

export function DeskPlan({ view, renderConfirm, renderRearm }: DeskPlanProps) {
  return (
    <div className="space-y-8" data-testid="twt-desk">
      <SessionStrip view={view} />
      <SweepBand view={view} />
      {view.expired ? (
        <p
          className="rounded-lg border border-negative/50 bg-negative-muted p-4 text-sm font-medium text-foreground"
          data-testid="twt-plan-expired"
        >
          This plan has expired. Rebuild it before acting on anything below — the prices, the sizes
          and the stops were all computed for a moment that has passed.
        </p>
      ) : (
        <p className="text-sm text-muted-foreground" data-testid="twt-plan-live">
          Plan {view.planReference} · {view.minutesLeft} minutes left before it has to be rebuilt.
        </p>
      )}

      {view.sections.map((section) => (
        <section
          key={section.id}
          aria-label={section.heading}
          data-testid={`twt-desk-${section.id}`}
          className="space-y-3"
        >
          <h2 className="text-sm font-medium uppercase tracking-wide text-muted-foreground">
            {section.heading}
          </h2>
          {section.lines.length === 0 ? (
            <p className="text-sm text-muted-foreground">{section.emptyReason}</p>
          ) : (
            <ul className="space-y-2">
              {section.lines.map((line) => (
                <LineRow
                  key={line.id}
                  line={line}
                  {...(renderConfirm ? { renderConfirm } : {})}
                />
              ))}
            </ul>
          )}
        </section>
      ))}

      <div data-testid="twt-desk-positions">
        <OpenPositions rows={view.positions} />
        {view.positions.some((position) => position.naked) && renderRearm ? (
          <ul className="mt-3 space-y-2" data-testid="twt-desk-rearm">
            {view.positions
              .filter((position) => position.naked)
              .map((position) => (
                <li key={position.id} className="flex items-center gap-3 text-sm">
                  <span className="font-medium">{position.symbol}</span>
                  <span className="text-muted-foreground">has no stop resting</span>
                  {renderRearm(position)}
                </li>
              ))}
          </ul>
        ) : null}
      </div>

      {view.skips.length > 0 ? (
        <section aria-label="Passed over" className="space-y-2" data-testid="twt-desk-skips">
          <h2 className="text-sm font-medium uppercase tracking-wide text-muted-foreground">
            Passed over, and why
          </h2>
          <ul className="space-y-1 text-sm text-muted-foreground">
            {view.skips.map((skip) => (
              <li key={skip.symbol}>
                <span className="font-medium text-foreground">{skip.symbol}</span> — {skip.reason}
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}

/** `05` §2.1 — the session, the mode as a badge, the gate and the counts that say what ran. */
function SessionStrip({ view }: { view: DeskView }) {
  const counts: ReadonlyArray<[string, number]> = [
    ["Quiet names", view.session.states],
    ["Entry signals", view.session.signals],
    ["Orders placed", view.session.orders_placed],
    ["Confirmed", view.session.confirms],
    ["Filled", view.session.fills],
    ["Stops raised", view.session.ratchets],
    ["Exits", view.session.exits],
  ];
  return (
    <section
      aria-label="This session"
      className="rounded-lg border border-border/70 bg-card p-4"
      data-testid="twt-desk-strip"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span
          data-testid="twt-desk-mode"
          className={cn(
            "rounded-md px-2 py-1 text-sm font-semibold uppercase tracking-wide",
            view.mode === "LIVE"
              ? "bg-negative-muted text-negative"
              : "bg-warning-muted text-warning",
          )}
        >
          {view.mode === "LIVE" ? "LIVE" : "DRY RUN"}
        </span>
        <span
          className={cn(
            "rounded-md px-2 py-1 text-sm font-semibold uppercase tracking-wide",
            view.gate === "OPEN" ? "bg-positive-muted text-positive" : "bg-muted text-muted-foreground",
          )}
        >
          Gate {view.gate}
        </span>
        <span className="text-sm text-muted-foreground">
          {formatTradeDate(view.session.date)}
        </span>
        {!view.executionEnabled ? (
          <span className="text-sm text-muted-foreground" data-testid="twt-desk-execution-off">
            · trading for this strategy is switched off
          </span>
        ) : null}
      </div>
      <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-4">
        {counts.map(([label, value]) => (
          <div key={label}>
            <dt className="text-xs uppercase tracking-wide text-muted-foreground">{label}</dt>
            <dd className="text-base font-medium tabular-nums">{value}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

/**
 * `05` §2's 15:15 band, and it stays until the sweep comes back clean.
 *
 * It is directly under the strip because an open position with nothing resting at the exchange is
 * the one state this method forbids, and a morning that runs out of attention should have seen it
 * before it saw anything else.
 */
function SweepBand({ view }: { view: DeskView }) {
  if (view.sweepNaked.length === 0) return null;
  return (
    <p
      className="rounded-lg border border-negative/50 bg-negative-muted p-4 text-sm font-medium text-foreground"
      data-testid="twt-sweep-band"
    >
      {view.sweepNaked.length === 1
        ? "One open position has no stop resting at the exchange"
        : `${view.sweepNaked.length} open positions have no stop resting at the exchange`}
      : {view.sweepNaked.join(", ")}. This band stays until every one of them is protected.
    </p>
  );
}

function LineRow({
  line,
  renderConfirm,
}: {
  line: DeskLineView;
  renderConfirm?: (line: DeskLineView) => ReactNode;
}) {
  const ratchet = line.kind === "RAISE_GTT_STOP";
  return (
    <li
      className="rounded-lg border border-border/60 bg-card p-3"
      data-testid="twt-desk-line"
      data-line-kind={line.kind}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-2">
        <div>
          <span className="font-medium">{line.symbol}</span>
          <span className="ml-2 text-sm text-muted-foreground">{line.action}</span>
        </div>
        <div>
          {line.confirmable && renderConfirm ? (
            renderConfirm(line)
          ) : (
            <span className="text-sm text-muted-foreground" data-testid="twt-line-state">
              {line.confirmable ? "awaiting the operator" : stateWords(line)}
            </span>
          )}
        </div>
      </div>
      <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-2 text-sm sm:grid-cols-4">
        {ratchet ? (
          <>
            <Cell label="Resting now" figure={line.oldTrigger} />
            <Cell label="Raise to" figure={line.stop} />
            <Cell label="Highest since buying" figure={line.highSince} />
            <Cell label="Room before the stop" figure={line.distanceToTrigger} />
          </>
        ) : line.kind === "ARM_GTT" ? (
          <>
            <Cell label="Place the stop at" figure={line.stop} />
            <Cell label="Highest since buying" figure={line.highSince} />
            <Cell label="Last price" figure={line.lastPrice} />
            <Cell label="Room before the stop" figure={line.distanceToTrigger} />
          </>
        ) : (
          <>
            <Cell label="Quantity" figure={line.quantity} />
            <Cell label="Value" figure={line.value} />
            <Cell label="Stop the fill gets" figure={line.stop} />
            <Cell label="Ranked by" figure={line.rankKey} />
          </>
        )}
      </dl>
      {line.capNote ? (
        <p className="mt-2 text-xs text-muted-foreground" data-testid="twt-line-cap">
          {line.capNote}
        </p>
      ) : null}
    </li>
  );
}

function Cell({ label, figure }: { label: string; figure: DeskLineView["stop"] }) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide text-muted-foreground">{label}</dt>
      <dd className="font-medium">
        <FigureValue figure={figure} />
      </dd>
    </div>
  );
}

/** What a line that cannot be confirmed says instead — never a disabled control. */
function stateWords(line: DeskLineView): string {
  switch (line.state) {
    case "CONFIRMED":
      return "confirmed";
    case "SENT":
      return "sent";
    case "FILLED":
      return "filled";
    case "REJECTED":
      return "rejected";
    case "EXPIRED":
      return "expired";
    case "SKIPPED":
      return "passed over";
    default:
      return "the plan expired before this was sent";
  }
}
