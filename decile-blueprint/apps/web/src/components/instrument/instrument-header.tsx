import type { IndexMembershipOut, InstrumentHeaderOut } from "@decile/api-client";

import { Badge } from "@/components/ui/badge";
import { formatTradeDate } from "@/lib/format";
import { formatCell } from "@/lib/instrument/present";

/**
 * docs/01 §5 block 1 — "symbol, ₹ price, full name, `NSE: SYMBOL`, index membership chips
 * (e.g. *Nifty Total Market*, *Nifty Microcap 250*)."
 *
 * The price is `close_raw`, the exchange print: CLAUDE.md house rule 6 — "display uses
 * `close_raw` where the user expects a real price". Every factor on the rest of the page is
 * computed from the adjusted `close`, which is why the as-of date is stated beside it rather than
 * left to be inferred.
 */
export interface InstrumentHeadingProps {
  header: InstrumentHeaderOut;
  memberships: readonly IndexMembershipOut[];
  asOf: string;
}

export function InstrumentHeading({ header, memberships, asOf }: InstrumentHeadingProps) {
  return (
    <header className="flex flex-col gap-3">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">{header.symbol}</h1>
        <p className="text-2xl font-semibold tnum">₹{formatCell("close_raw", header.close_raw)}</p>
        <p className="text-sm text-muted-foreground">as of {formatTradeDate(asOf)}</p>
      </div>

      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <p className="text-sm text-muted-foreground">{header.name}</p>
        <p className="text-xs text-muted-foreground">
          {header.exchange}: {header.symbol}
        </p>
      </div>

      {memberships.length > 0 ? (
        <ul aria-label="Index membership" className="flex flex-wrap gap-1.5">
          {memberships.map((index) => (
            <li key={index.slug}>
              <Badge variant="outline">{index.name}</Badge>
            </li>
          ))}
        </ul>
      ) : null}
    </header>
  );
}
