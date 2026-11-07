import type { Route } from "next";
import Link from "next/link";
import type { ReactNode } from "react";

import { SymbolSeriesFlag } from "@/components/regime-signal/series-flag";
import { hrefFor } from "@/lib/search/hrefs";
import { cn } from "@/lib/utils";

/** `/instruments/SHADOWFAX` — the same href search and overlap already mint. */
export function instrumentHref(symbol: string): Route {
  return hrefFor({ kind: "instrument", id: symbol.trim().toUpperCase() });
}

export function InstrumentLink({
  symbol,
  className,
  children,
  flag = true,
}: {
  symbol: string;
  className?: string;
  children?: ReactNode;
  /** Off where the caller already shows the series flag. */
  flag?: boolean;
}) {
  const trimmed = symbol.trim();
  if (!trimmed || trimmed === "—") {
    return <span className={className}>{children ?? symbol}</span>;
  }
  const ticker = trimmed.toUpperCase();
  const link = (
    <Link
      href={instrumentHref(ticker)}
      className={cn("font-medium text-foreground underline-offset-4 hover:underline", className)}
    >
      {children ?? ticker}
    </Link>
  );
  return flag ? (
    <>
      {link}
      <SymbolSeriesFlag symbol={ticker} />
    </>
  ) : (
    link
  );
}
