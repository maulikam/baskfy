import { InstrumentLink } from "@/components/instrument/instrument-link";
import {
  EMPTY_CELL,
  formatFraction,
  formatNumber,
  formatTradeDate,
} from "@/lib/format";
import type { MbName } from "@/lib/mb/fetch";

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
        No investable name was scored on {formatTradeDate(asOf)}.
      </p>
    );
  }
  return (
    <div className="space-y-2">
      <p
        className="max-w-[80ch] text-xs text-muted-foreground"
        data-testid="mb-rank-note"
      >
        Rank counts investable names only: a close of at least ₹50 and at least
        ₹5 cr traded a day, the floor the backtest bought at. Overall is the
        model&rsquo;s rank among every name it scored; the gaps are names under
        that floor, which it often likes and the book never buys.
      </p>
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
                "Overall",
                "Stock",
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
              ].map((h) => (
                <th key={h} scope="col" className="py-2 pr-3 font-medium">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {names.map((n, k) => (
              <tr
                key={n.instrument_id}
                className="border-b border-border/40"
                data-testid="mb-row"
              >
                <td className="py-2 pr-3 tabular-nums" data-testid="mb-rank">
                  {k + 1}
                </td>
                <td
                  className="py-2 pr-3 tabular-nums text-muted-foreground"
                  data-testid="mb-overall"
                >
                  {n.rank}
                </td>
                <td className="py-2 pr-3">
                  <InstrumentLink symbol={n.symbol} flag={false} />
                  {n.investable ? null : (
                    <span
                      className="ml-1.5 text-xs text-muted-foreground"
                      title="Under ₹50 or under ₹5 cr a day"
                    >
                      not investable
                    </span>
                  )}
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
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
