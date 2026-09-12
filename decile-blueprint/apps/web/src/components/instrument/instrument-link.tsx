import type { Route } from "next";
import Link from "next/link";
import type { MouseEvent, ReactNode } from "react";

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
  onClick,
}: {
  symbol: string;
  className?: string;
  children?: ReactNode;
  onClick?: (event: MouseEvent<HTMLAnchorElement>) => void;
}) {
  const trimmed = symbol.trim();
  if (!trimmed || trimmed === "—") {
    return <span className={className}>{children ?? symbol}</span>;
  }
  const ticker = trimmed.toUpperCase();
  return (
    <Link
      href={instrumentHref(ticker)}
      className={cn("font-medium text-foreground underline-offset-4 hover:underline", className)}
      onClick={onClick}
    >
      {children ?? ticker}
    </Link>
  );
}
