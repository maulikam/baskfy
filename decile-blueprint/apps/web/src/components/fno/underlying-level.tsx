"use client";

import { useLiveMarksContext } from "@/components/screens/live-price";
import { dayMonth, liveLabel, price } from "@/lib/fno/view";

/**
 * The underlying's level on an F1 card — **the one number on `/options/overnight` the shared live
 * overlay touches** (`docs/fno/05` §2's clock table: `NIFTY live 13:14`). Its base is the index
 * level the bhavcopy printed beside the session's futures; outside hours, or without a Kite
 * session, it stays that close and says so. Legs, marks and the condor never go live here.
 */

const IST_HHMM = new Intl.DateTimeFormat("en-GB", {
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
  timeZone: "Asia/Kolkata",
});

export function UnderlyingLevel({
  symbol,
  liveSymbol,
  level,
  closeOf,
}: {
  symbol: string;
  liveSymbol: string;
  level: string | null;
  closeOf: string | null;
}) {
  const live = useLiveMarksContext();
  const quote = live.liveOverlay
    ? live.quotes[liveSymbol.toUpperCase()]
    : undefined;
  if (quote !== undefined) {
    return (
      <span className="flex flex-col" data-testid="fno-level" data-live="true">
        <span className="text-base font-semibold tabular-nums">
          {quote.lastPrice.toFixed(2)}
        </span>
        <span className="text-xs text-positive">
          {liveLabel(symbol, IST_HHMM.format(new Date()))}
        </span>
      </span>
    );
  }
  return (
    <span className="flex flex-col" data-testid="fno-level" data-live="false">
      <span className="text-base font-semibold tabular-nums">
        {level === null ? "not in the file" : price(level)}
      </span>
      <span className="text-xs text-muted-foreground">
        {closeOf
          ? `${symbol} close, ${dayMonth(closeOf)}`
          : `${symbol} level not stored yet`}
      </span>
    </span>
  );
}
