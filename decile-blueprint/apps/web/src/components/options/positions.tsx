import type {
  OptionsClosedTrade,
  OptionsPosition,
  OptionsWeekR,
} from "@/lib/options/types";
import {
  SLEEVE_SHORT,
  inr,
  istTime,
  level,
  reasonText,
  structureName,
} from "@/lib/options/view";

/**
 * `05` §2's fourth panel: every open position with its entry, its mark and the minutes to its
 * hard exit; today's closed trades; each sleeve's running R for the week. Marks are **the desk's
 * persisted mark** (`Marked 13:14:30`) — the page never prices a position itself. Real and
 * simulated are never summed together.
 */
export function PositionsPanel({
  positions,
  closed,
  weekR,
}: {
  positions: readonly OptionsPosition[];
  closed: readonly OptionsClosedTrade[];
  weekR: readonly OptionsWeekR[];
}) {
  return (
    <section
      aria-labelledby="options-positions-heading"
      className="space-y-3 rounded-lg border border-border/70 bg-card p-4"
      data-testid="options-positions"
    >
      <h2 id="options-positions-heading" className="text-base font-semibold">
        Positions and paper results
      </h2>
      {positions.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No options position is open. Every position here would be a paper one
          — no options strategy trades real money.
        </p>
      ) : (
        <ul className="space-y-2 text-sm">
          {positions.map((p) => (
            <li
              key={p.session_id}
              className="flex flex-wrap justify-between gap-x-4"
            >
              <span>
                {SLEEVE_SHORT[p.sleeve]} · {p.lots} lot{p.lots === 1 ? "" : "s"}{" "}
                · {p.simulated ? "paper" : "real"}
              </span>
              <span className="tabular-nums">
                entry {level(p.entry_points)} · mark {level(p.last_mark_points)}{" "}
                <span className="text-xs text-muted-foreground">
                  {p.last_mark_at
                    ? `Marked ${istTime(p.last_mark_at, { seconds: true })}`
                    : "not marked yet"}
                </span>
                {p.minutes_to_hard_exit !== null
                  ? ` · ${p.minutes_to_hard_exit} min to the hard exit`
                  : ""}
              </span>
            </li>
          ))}
        </ul>
      )}
      {closed.length > 0 ? (
        <div>
          <p className="text-xs text-muted-foreground">Closed today</p>
          <ul className="space-y-1 text-sm">
            {closed.map((c) => (
              <li
                key={c.session_id}
                className="flex flex-wrap justify-between gap-x-4 tabular-nums"
              >
                <span>
                  {SLEEVE_SHORT[c.sleeve]} · {structureName(c.structure)} ·{" "}
                  {reasonText(c.closed_reason).toLowerCase()}
                </span>
                <span>
                  {inr(c.net_pnl_inr)} · {c.r_multiple} R ·{" "}
                  {c.simulated ? "paper" : "real"}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      <p className="text-sm text-muted-foreground" data-testid="options-week-r">
        {weekR.length === 0
          ? "No trade has closed this week."
          : `This week: ${weekR
              .map(
                (w) =>
                  `${SLEEVE_SHORT[w.sleeve]} ${w.r} R over ${w.trades} (${w.simulated ? "paper" : "real"})`,
              )
              .join(" · ")}.`}
      </p>
    </section>
  );
}
