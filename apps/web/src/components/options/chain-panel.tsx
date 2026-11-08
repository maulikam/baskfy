import { Disclosure } from "@/components/twt/disclosure";
import type { OptionsChain } from "@/lib/options/types";
import { istTime, level, sessionDay } from "@/lib/options/view";

/**
 * `05` §2's chain panel — **collapsed by default**: supporting data, not a strategy. The nearest
 * two expiries' strikes around the money with bid/ask, last, OI and its change since the open,
 * IV and the greeks the collector computed (Black-76 on the parity forward, OP3). ATM IV and the
 * put–call OI ratio are numbers; v1 draws no chart of them.
 */

function pctIv(value: string | null): string {
  if (value === null) return "not computed";
  const n = Number(value);
  return Number.isFinite(n) ? `${(n * 100).toFixed(1)}%` : "not computed";
}

function greek(value: string | null, digits = 2): string {
  if (value === null) return "not computed";
  const n = Number(value);
  return Number.isFinite(n) ? n.toFixed(digits) : "not computed";
}

export function ChainPanel({ chain }: { chain: OptionsChain | null }) {
  const expiries = chain?.expiries ?? [];
  return (
    <Disclosure summary="The option chain" testId="options-chain">
      {expiries.length === 0 ? (
        <p>
          No option chain has been collected yet — the collector is what reads
          it, once a minute.
        </p>
      ) : (
        expiries.map((expiry) => (
          <div key={expiry.expiry} className="space-y-2 text-foreground">
            <p className="text-sm font-medium">
              Expiry {sessionDay(expiry.expiry)} · minute {istTime(expiry.ts)} ·
              ATM {level(expiry.atm_strike)} · ATM IV {pctIv(expiry.atm_iv)} ·
              put–call OI ratio {expiry.pcr_oi ?? "not computed"}
            </p>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[40rem] text-xs tabular-nums">
                <caption className="sr-only">
                  Strikes around the money for this expiry
                </caption>
                <thead className="text-left text-muted-foreground">
                  <tr>
                    {[
                      "Strike",
                      "Type",
                      "Bid",
                      "Ask",
                      "Last",
                      "OI",
                      "OI change",
                      "IV",
                      "Delta",
                      "Gamma",
                      "Theta",
                    ].map((heading) => (
                      <th
                        key={heading}
                        scope="col"
                        className="py-1 pr-2 font-normal"
                      >
                        {heading}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {expiry.rows.map((row) => (
                    <tr
                      key={`${row.strike}-${row.option_type}`}
                      className="border-t border-border/40"
                    >
                      <td className="py-0.5 pr-2">{level(row.strike)}</td>
                      <td className="pr-2">{row.option_type}</td>
                      <td className="pr-2">{level(row.bid)}</td>
                      <td className="pr-2">{level(row.ask)}</td>
                      <td className="pr-2">{level(row.last)}</td>
                      <td className="pr-2">
                        {row.oi?.toLocaleString("en-IN") ?? "not quoted"}
                      </td>
                      <td className="pr-2">
                        {row.oi_change?.toLocaleString("en-IN") ?? "not yet"}
                      </td>
                      <td className="pr-2">{pctIv(row.iv)}</td>
                      <td className="pr-2">{greek(row.delta)}</td>
                      <td className="pr-2">{greek(row.gamma, 4)}</td>
                      <td className="pr-2">{greek(row.theta)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ))
      )}
    </Disclosure>
  );
}
