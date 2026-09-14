import { Suspense, type ReactNode } from "react";

import { AppearancesStrip } from "@/components/instrument/appearances-strip";
import { SaveInstrumentButton } from "@/components/watchlist/save-instrument-button";

/**
 * Adds the instrument Save control above the factsheet without editing the page (AF I.2).
 * Screen-table wiring remains STATUS: needs screen results table.
 */

export default async function InstrumentSymbolLayout({
  children,
  params,
}: {
  children: ReactNode;
  params: Promise<{ symbol: string }>;
}) {
  const { symbol } = await params;
  return (
    <div className="flex flex-col gap-4">
      <div className="flex justify-end">
        <SaveInstrumentButton symbol={symbol.toUpperCase()} />
      </div>
      {/* Its own request, streamed: the factsheet never waits on the caller's screens. */}
      <Suspense fallback={null}>
        <AppearancesStrip symbol={symbol.toUpperCase()} />
      </Suspense>
      {children}
    </div>
  );
}
