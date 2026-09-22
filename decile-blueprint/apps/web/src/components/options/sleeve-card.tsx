import type {
  Candidate,
  OptionsRole,
  OptionsScan,
  SleeveCode,
} from "@/lib/options/types";
import {
  SLEEVE_NAME,
  SLEEVE_SHORT,
  candidates as candidatesOf,
  checks,
  counterTrendBreaks,
  fractionPct,
  inr,
  legLine,
  level,
  nextDate,
  reasonText,
  roleLine,
  sessionDay,
  sizingText,
  structureName,
  trigger,
} from "@/lib/options/view";
import { cn } from "@/lib/utils";

import { StateChip } from "./state-chip";

/**
 * One sleeve's card (`05` §2's four panels share this shape): the state chip, every reason, the
 * filters against their thresholds, and the candidate the scan priced — the same computation the
 * desk's plan builder will run, from the same minute (OP4.4), shown as **paper**.
 */

function CandidateTable({ candidate }: { candidate: Candidate }) {
  return (
    <div className="space-y-2" data-testid="options-candidate">
      <p className="text-sm font-medium">
        {structureName(candidate.structure)} · expiry{" "}
        {sessionDay(candidate.expiry)}
        {candidate.direction
          ? ` · ${candidate.direction === "UP" ? "up" : "down"}`
          : ""}
      </p>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[28rem] text-sm tabular-nums">
          <caption className="sr-only">The candidate&rsquo;s legs</caption>
          <thead className="text-left text-xs text-muted-foreground">
            <tr>
              <th scope="col" className="py-1 pr-3 font-normal">
                Leg
              </th>
              <th scope="col" className="py-1 pr-3 font-normal">
                Bid / ask
              </th>
              <th scope="col" className="py-1 pr-3 font-normal">
                Delta
              </th>
              <th scope="col" className="py-1 font-normal">
                Limit
              </th>
            </tr>
          </thead>
          <tbody>
            {candidate.legs.map((leg) => (
              <tr
                key={`${leg.side}-${leg.strike}-${leg.option_type}`}
                className="border-t border-border/50"
              >
                <td className="py-1 pr-3">{legLine(leg)}</td>
                <td className="py-1 pr-3">
                  {level(leg.bid)} / {level(leg.ask)}
                </td>
                <td className="py-1 pr-3">
                  {leg.delta ? Number(leg.delta).toFixed(2) : "not quoted"}
                </td>
                <td className="py-1">{level(leg.limit_price)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm sm:grid-cols-4">
        <div>
          <dt className="text-xs text-muted-foreground">
            {candidate.structure === "IRON_CONDOR"
              ? "Credit (points)"
              : "Premium (points)"}
          </dt>
          <dd className="tabular-nums">{level(candidate.points)}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">Lots</dt>
          <dd className="tabular-nums">
            {candidate.lots ?? 0} × {candidate.lot_size ?? "lot"} ·{" "}
            {sizingText(candidate.sizing_mode)}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">Most it can lose</dt>
          <dd className="tabular-nums">{inr(candidate.max_loss_inr)}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">Round-trip cost</dt>
          <dd className="tabular-nums">
            {inr(candidate.round_trip_inr)} ·{" "}
            {fractionPct(candidate.cost_share)} of the gain
          </dd>
        </div>
      </dl>
      {candidate.rejection ? (
        <p
          className="text-sm text-warning"
          data-testid="options-candidate-rejected"
        >
          Would not be placed: {reasonText(candidate.rejection).toLowerCase()}.
        </p>
      ) : null}
    </div>
  );
}

export function SleeveCard({
  sleeve,
  scan,
  role,
}: {
  sleeve: SleeveCode;
  scan: OptionsScan | null;
  role: OptionsRole | undefined;
}) {
  const rows = scan ? checks(scan) : [];
  const trig = scan ? trigger(scan) : null;
  const seen = scan ? counterTrendBreaks(scan) : [];
  const priced = scan ? candidatesOf(scan) : [];
  const next = scan ? nextDate(scan) : (role?.next_date ?? null);
  return (
    <article
      className="space-y-3 rounded-lg border border-border/70 bg-card p-4"
      data-testid={`options-sleeve-${sleeve}`}
      aria-labelledby={`options-sleeve-${sleeve}-heading`}
    >
      <header className="flex flex-wrap items-center justify-between gap-2">
        <h3
          id={`options-sleeve-${sleeve}-heading`}
          className="text-base font-semibold"
        >
          {SLEEVE_NAME[sleeve]}{" "}
          <span className="text-sm font-normal text-muted-foreground">
            ({SLEEVE_SHORT[sleeve]})
          </span>
        </h3>
        {scan ? <StateChip state={scan.state} /> : null}
      </header>

      {scan === null ? (
        <p
          className="text-sm text-muted-foreground"
          data-testid="options-sleeve-unscanned"
        >
          Not scanned
          {role
            ? ` — ${roleLine(role).replace(`${SLEEVE_SHORT[sleeve]}: `, "")}`
            : ""}
          .
        </p>
      ) : null}

      {scan && scan.reasons.length > 0 ? (
        <ul
          className="list-disc space-y-0.5 pl-5 text-sm"
          data-testid="options-reasons"
        >
          {scan.reasons.map((reason) => (
            <li key={reason}>{reasonText(reason)}</li>
          ))}
        </ul>
      ) : null}

      {scan && scan.state === "NOT_TODAY" && next ? (
        <p className="text-sm text-muted-foreground">
          Next session {sessionDay(next)}.
        </p>
      ) : null}

      {rows.length > 0 ? (
        <dl className="grid gap-1 text-sm" data-testid="options-checks">
          {rows.map((row) => (
            <div
              key={row.label}
              className="flex flex-wrap justify-between gap-x-4"
            >
              <dt className="text-muted-foreground">{row.label}</dt>
              <dd
                className={cn(
                  "tabular-nums",
                  row.ok === true && "text-positive",
                  row.ok === false && "text-negative",
                )}
              >
                {row.value}
                {row.limit ? (
                  <span className="ml-1 text-xs text-muted-foreground">
                    ({row.limit})
                  </span>
                ) : null}
              </dd>
            </div>
          ))}
        </dl>
      ) : null}

      {trig ? (
        <p className="text-sm" data-testid="options-trigger">
          Trigger at <strong className="tabular-nums">{trig.level}</strong>
          {trig.points === "reached"
            ? " — reached."
            : ` — ${level(trig.points)} points away (${trig.pct}).`}
        </p>
      ) : null}

      {seen.length > 0 ? (
        <div className="text-sm" data-testid="options-seen-not-traded">
          <p className="text-muted-foreground">
            Seen, not traded (against the trend):
          </p>
          <ul className="pl-5 tabular-nums">
            {seen.map((b) => (
              <li key={`${b.time}-${b.close}`}>
                {b.time} close {b.close}, {b.direction}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {priced.map((candidate, index) => (
        <CandidateTable
          key={`${candidate.structure}-${index}`}
          candidate={candidate}
        />
      ))}
    </article>
  );
}
