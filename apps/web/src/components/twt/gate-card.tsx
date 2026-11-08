import { formatTradeDate } from "@/lib/format";
import {
  NO_FUNNEL,
  THIN_SESSION,
  breadthLine,
  funnelSteps,
  gateMeaning,
} from "@/lib/twt/copy";
import type { TwtGate } from "@/lib/twt/fetch";
import { cn } from "@/lib/utils";

import { Disclosure } from "./disclosure";

/**
 * `docs/twt/05` §1.1 — may this strategy open a new position at all, and what does that mean.
 *
 * It is the first card on the page because a list of tight names under a shut gate is a list of
 * trades not to take, and reading the list first is how a person ends up taking one.
 *
 * **The SHUT sentence is the one that had to be written carefully.** `05` §1.1: "A reader must
 * never have to infer that exits keep running." SHUT means no new entries; it does not mean sell
 * anything, and a person who reads it as "get out" has been misled by a single word.
 */
export function GateCard({ gate }: { gate: TwtGate | null }) {
  const state = gate?.gate ?? null;
  const steps = gate === null ? [] : funnelSteps(gate);

  return (
    <section aria-labelledby="twt-gate-heading" className="space-y-3">
      <h2
        id="twt-gate-heading"
        className="text-sm font-medium uppercase tracking-wide text-muted-foreground"
      >
        Whether new entries are allowed today
      </h2>

      <div className="rounded-lg border border-border/70 bg-card p-4">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
          <span
            data-testid="twt-gate-badge"
            className={cn(
              "rounded-md px-2 py-1 text-sm font-semibold uppercase tracking-wide",
              state === "OPEN" && "bg-positive-muted text-positive",
              state === "SHUT" && "bg-warning-muted text-warning",
              state === null && "bg-muted text-muted-foreground",
            )}
          >
            {state === null ? "Not read yet" : state}
          </span>
          {gate?.date ? (
            <span className="text-sm text-muted-foreground">
              read on {formatTradeDate(gate.date)}
            </span>
          ) : null}
        </div>

        <p className="mt-3 max-w-[70ch] text-sm leading-relaxed" data-testid="twt-gate-meaning">
          {state === null
            ? "The market breadth this strategy uses has not been read for a session yet, so it cannot say whether a new entry would be allowed."
            : `${gateMeaning(state)}.`}
        </p>

        {gate && state !== null ? (
          <p className="mt-2 max-w-[70ch] text-sm text-muted-foreground">{breadthLine(gate)}.</p>
        ) : null}

        {gate?.thin_session ? (
          <p className="mt-2 max-w-[70ch] text-sm text-warning" data-testid="twt-thin-session">
            {THIN_SESSION}
          </p>
        ) : null}
      </div>

      <Disclosure summary="How that reading is taken" testId="twt-funnel">
        {steps.length === 0 ? (
          <p>{NO_FUNNEL}</p>
        ) : (
          <ul className="space-y-1">
            {steps.map((step) => (
              <li key={step.label} className="flex justify-between gap-4">
                <span>{step.label}</span>
                <span className="tabular-nums text-foreground">{step.value}</span>
              </li>
            ))}
          </ul>
        )}
        <p>
          Each step is a subset of the one above it. The last two are what the percentage is: how
          many of the names with a full year of prices behind them are trading above their own
          200-day average.
        </p>
      </Disclosure>
    </section>
  );
}
