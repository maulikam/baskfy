import { LiveMarksProvider, LivePrice } from "@/components/screens/live-price";
import { formatTradeDate } from "@/lib/format";
import type {
  OptionsIndexLevel,
  OptionsToday,
  SleeveGroupCode,
} from "@/lib/options/types";
import {
  GROUP_NAME,
  istTime,
  level,
  reasonText,
  roleLine,
} from "@/lib/options/view";
import { cn } from "@/lib/utils";

/**
 * `05` §2's header strip: today's role per sleeve, the next six expiries from the NFO master
 * (`op_expiry`) with event days marked, the book's pause state, and the NIFTY 50 / India VIX
 * levels.
 *
 * **The two index levels are the only numbers on the tab the shared live overlay touches**
 * (`useLiveMarks`, the screens' one query, polled only while the market is open and a Kite
 * session exists — DECISIONS-OP OP5.3). Their base is the collector's last minute, else the last
 * daily close, and each says which. Every scan number below stays on the collector's minute.
 */

const INDEX_SYMBOLS = ["NIFTY 50", "INDIA VIX"] as const;

function IndexCell({
  label,
  value,
}: {
  label: string;
  value: OptionsIndexLevel;
}) {
  const basis = value.at
    ? `minute ${istTime(value.at)}`
    : value.close_of
      ? `close ${formatTradeDate(value.close_of)}`
      : "not collected yet";
  return (
    <div
      className="flex flex-col"
      data-testid={`options-index-${label === "NIFTY 50" ? "nifty" : "vix"}`}
    >
      <span className="text-xs text-muted-foreground">{label}</span>
      <LivePrice
        symbol={value.symbol}
        fallback={
          <span className="text-base font-semibold tabular-nums">
            {value.level === null ? "not collected yet" : level(value.level)}
          </span>
        }
        className="text-base font-semibold"
      />
      <span className="text-xs text-muted-foreground">{basis}</span>
    </div>
  );
}

export function HeaderStrip({ today }: { today: OptionsToday }) {
  const paused = today.pauses.filter((pause) => pause.paused_until !== null);
  return (
    <section aria-labelledby="options-header-heading" className="space-y-3">
      <h2 id="options-header-heading" className="sr-only">
        Today&rsquo;s roles, expiries and the index
      </h2>
      <ul className="flex flex-wrap gap-2" data-testid="options-roles">
        {today.roles.map((role) => (
          <li
            key={role.sleeve}
            className={cn(
              "rounded-md border px-2 py-1 text-xs",
              role.today
                ? "border-accent/50 bg-accent-muted text-accent"
                : "border-border text-muted-foreground",
            )}
            title={reasonText(role.reason)}
          >
            {roleLine(role)}
          </li>
        ))}
      </ul>

      <div className="grid gap-3 rounded-lg border border-border/70 bg-card p-4 sm:grid-cols-[auto_1fr]">
        <LiveMarksProvider symbols={INDEX_SYMBOLS}>
          <div className="flex gap-6">
            <IndexCell label="NIFTY 50" value={today.nifty} />
            <IndexCell label="India VIX" value={today.vix} />
          </div>
        </LiveMarksProvider>
        <div>
          <p className="text-xs text-muted-foreground">Next NIFTY expiries</p>
          {today.expiries.length === 0 ? (
            <p className="text-sm" data-testid="options-no-expiries">
              The contract list has not been loaded yet, so no expiry dates are
              known.
            </p>
          ) : (
            <ol
              className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-sm"
              data-testid="options-expiries"
            >
              {today.expiries.map((expiry) => (
                <li key={expiry.expiry_date} className="tabular-nums">
                  {formatTradeDate(expiry.expiry_date)}
                  <span className="ml-1 text-xs text-muted-foreground">
                    {expiry.kind === "MONTHLY" ? "monthly" : "weekly"} · lot{" "}
                    {expiry.lot_size}
                  </span>
                  {expiry.event_day ? (
                    <span className="ml-1 text-xs text-warning">
                      · event day, no strategy trades
                    </span>
                  ) : null}
                </li>
              ))}
            </ol>
          )}
        </div>
      </div>

      <p className="text-sm" data-testid="options-pause">
        {paused.length === 0
          ? "No options strategy is paused."
          : paused
              .map((pause) =>
                /* A pause row is either one strategy group's or the account's (it covers every
                   strategy at once) — anything that is not a group is the account. */
                pause.scope in GROUP_NAME
                  ? `${GROUP_NAME[pause.scope as SleeveGroupCode]} is paused until ${formatTradeDate(pause.paused_until)}`
                  : `The whole options account is paused until ${formatTradeDate(pause.paused_until)}`,
              )
              .join(" · ")}
      </p>
    </section>
  );
}
