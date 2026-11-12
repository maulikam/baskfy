import { InstrumentLink } from "@/components/instrument/instrument-link";
import { FlagTip } from "@/components/mb/flag-tip";
import { BuyTicketButton } from "@/components/orders/buy-ticket";
import {
  EMPTY_CELL,
  formatFraction,
  formatNumber,
  formatTradeDate,
} from "@/lib/format";
import type { MbName } from "@/lib/mb/fetch";
import { MB_FLAGS, SWING_SCOPE, flagsOf } from "@/lib/mb/flags";

const SYMBOL = Object.fromEntries(MB_FLAGS.map((f) => [f.key, f]));

const SESSIONS_A_YEAR = 250;

function turnoverCrore(rupees: string | null): string {
  if (rupees === null) return EMPTY_CELL;
  return formatNumber(Number(rupees) / 1e7, { decimals: 1, suffix: " cr" });
}

function listedYears(sessions: number | null): string {
  if (sessions === null) return EMPTY_CELL;
  return formatNumber(sessions / SESSIONS_A_YEAR, {
    decimals: 1,
    suffix: " y",
  });
}

/** The day's ranked names (PM7). Every price is the scored session's close; nothing here is live. */
export function MbRanking({
  names,
  asOf,
}: {
  names: readonly MbName[];
  asOf: string | null;
}) {
  if (asOf === null) {
    return (
      <p className="text-sm text-muted-foreground" data-testid="mb-unscored">
        No session has been scored yet. The ranking is written after the nightly
        publishes; until the first night with a published model has run, this is
        empty because nothing has been scored, not because nothing qualified.
      </p>
    );
  }
  if (names.length === 0) {
    return (
      <p className="text-sm text-muted-foreground" data-testid="mb-empty">
        No name was scored on {formatTradeDate(asOf)}.
      </p>
    );
  }
  return (
    <div className="space-y-2">
      <dl
        className="grid max-w-[90ch] gap-x-6 gap-y-1 text-xs text-muted-foreground sm:grid-cols-2"
        data-testid="mb-flag-key"
      >
        {MB_FLAGS.map((f) => (
          <div key={f.key} className="flex gap-2">
            <dt className={`w-5 shrink-0 text-center text-sm ${f.tone}`}>
              {f.symbol}
            </dt>
            <dd>
              <span className="font-medium text-foreground">{f.label}.</span>{" "}
              {f.meaning}
            </dd>
          </div>
        ))}
      </dl>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[64rem] border-collapse text-sm">
          <caption className="sr-only">
            Names ranked by the model&rsquo;s estimate of touching twice their
            price within a year, closes of {formatTradeDate(asOf)}
          </caption>
          <thead>
            <tr className="border-b border-border/70 text-left text-xs uppercase tracking-wide text-muted-foreground">
              {[
                "Rank",
                "Stock",
                "Flags",
                "Sector",
                "Close",
                "Traded a day",
                "12 months",
                "From its high",
                "Listed",
                "Revenue TTM",
                "Profit TTM",
                "63-day high",
                "Score",
                "Buy",
              ].map((h) => (
                <th key={h} scope="col" className="py-2 pr-3 font-medium">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {names.map((n) => (
              <tr
                key={n.instrument_id}
                className="border-b border-border/40"
                data-testid="mb-row"
              >
                <td
                  className={`py-2 pr-3 tabular-nums ${n.investable ? "font-medium text-emerald-600 dark:text-emerald-400" : ""}`}
                  data-testid="mb-rank"
                >
                  {n.rank}
                </td>
                <td className="py-2 pr-3">
                  <InstrumentLink symbol={n.symbol} flag={false} />
                </td>
                <td
                  className="py-2 pr-3 whitespace-nowrap"
                  data-testid="mb-flags"
                >
                  {flagsOf(n).map((k) => (
                    <FlagTip
                      key={k}
                      label={k === "outside" && n.scope && n.scope !== "universe" ? SWING_SCOPE[n.scope].label : (SYMBOL[k]?.label ?? k)}
                      meaning={k === "outside" && n.scope && n.scope !== "universe" ? SWING_SCOPE[n.scope].meaning : SYMBOL[k]?.meaning}
                      className={`mr-1.5 ${SYMBOL[k]?.tone ?? ""}`}
                    >
                      {SYMBOL[k]?.symbol}
                    </FlagTip>
                  ))}
                </td>
                <td className="py-2 pr-3 text-muted-foreground">
                  {n.sector ?? EMPTY_CELL}
                </td>
                <td className="py-2 pr-3 tabular-nums">
                  {formatNumber(n.close, { decimals: 2 })}
                </td>
                <td className="py-2 pr-3 tabular-nums">
                  {turnoverCrore(n.turnover20)}
                </td>
                <td className="py-2 pr-3 tabular-nums">
                  {formatFraction(n.r252_fraction, 0)}
                </td>
                <td className="py-2 pr-3 tabular-nums">
                  {formatFraction(n.dist_ath_fraction, 0)}
                </td>
                <td className="py-2 pr-3 tabular-nums">
                  {listedYears(n.sessions_listed)}
                </td>
                <td className="py-2 pr-3 tabular-nums">
                  {formatFraction(n.ttm_rev_yoy_fraction, 0)}
                </td>
                <td className="py-2 pr-3 tabular-nums">
                  {formatFraction(n.ttm_pat_yoy_fraction, 0)}
                </td>
                <td className="py-2 pr-3" data-testid="mb-hi63">
                  {n.hi63_trigger
                    ? formatTradeDate(n.hi63_trigger_date)
                    : EMPTY_CELL}
                </td>
                <td className="py-2 pr-3 tabular-nums text-muted-foreground">
                  {n.score}
                </td>
                <td className="py-2 pr-3">
                  <BuyTicketButton
                    symbol={n.symbol}
                    name={n.name}
                    instrumentId={n.instrument_id}
                    price={n.close === null ? null : Number(n.close)}
                    source="doubling"
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
