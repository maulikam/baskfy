"use client";

import dynamic from "next/dynamic";

import { Skeleton } from "@/components/ui/skeleton";

/**
 * `BreadthHistory`, loaded on demand — Prompt 16 deliverable 5, "dynamic import of charts".
 *
 * `/market-health` renders four of these below four gauges. The gauges are the page's answer to
 * docs/01 §6 and are above the fold; the charts are docs/08's addition ("a history chart of each
 * breadth series with the Nifty overlaid") and are not. Loading visx with the rest of the route
 * puts the charting code in front of the gauges for every visitor.
 *
 * A separate `"use client"` module rather than a `dynamic()` call in the page, because the page is
 * a Server Component (`docs/08` §Routes: "RSC + client universe switcher") and `ssr: false` is not
 * available there. `ssr: false` matters: a chart sizes itself against the viewport, so rendering
 * one on the server and re-rendering it on hydration is a layout shift, which `docs/08` budgets at
 * CLS < 0.1.
 *
 * The four skeletons keep the page's height stable while the chunk arrives, for the same reason.
 */
export const BreadthHistory = dynamic(
  () => import("@/components/market/breadth-history").then((m) => m.BreadthHistory),
  { ssr: false, loading: () => <Skeleton className="h-72 w-full" /> },
);

export type { BreadthKey, BreadthHistoryProps } from "@/components/market/breadth-history";
