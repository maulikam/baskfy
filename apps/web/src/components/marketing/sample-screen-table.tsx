import Link from "next/link";

import type { SampleScreen } from "@/lib/marketing/sample-screen";
import {
  SAMPLE_SCREEN_FACTOR,
  SAMPLE_SCREEN_NAME,
  SAMPLE_SCREEN_UNIVERSE,
} from "@/lib/marketing/sample-screen";
import { formatTradeDate } from "@/lib/format";

/**
 * The live sample on the landing page.
 *
 * A plain `<table>`, not the app's `DataTable`: eight rows need no virtualiser, no column sizing
 * and no sort state, and pulling TanStack Table onto the landing page would put ~40 KB of client
 * JS on the one route where Prompt 18 asks for a Lighthouse performance score. This component
 * ships zero bytes of JavaScript.
 */
export function SampleScreenTable({ sample }: { sample: SampleScreen | null }) {
  if (sample === null) {
    return (
      <div className="rounded-lg border border-dashed border-border p-6 text-sm text-muted-foreground">
        The sample screen could not be loaded — the data service did not answer when this page was
        built. Nothing stale is shown in its place.
      </div>
    );
  }

  const [first] = sample.rows;

  return (
    <figure className="space-y-3">
      <div className="overflow-x-auto rounded-lg border border-border">
        <table className="w-full min-w-[46rem] border-collapse text-sm tabular-nums">
          <caption className="sr-only">
            {SAMPLE_SCREEN_NAME}: the top {sample.rows.length} of {sample.resultCount} results as of{" "}
            {formatTradeDate(sample.asOf)}
          </caption>
          <thead>
            <tr className="border-b border-border bg-muted/40 text-xs uppercase tracking-wide text-muted-foreground">
              <th scope="col" className="px-3 py-2 text-right font-medium">
                #
              </th>
              <th scope="col" className="px-3 py-2 text-left font-medium">
                Symbol
              </th>
              {first?.cells.map((cell) => (
                <th key={cell.key} scope="col" className="px-3 py-2 text-right font-medium">
                  {cell.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {sample.rows.map((row) => (
              <tr key={row.symbol} className="border-b border-border/60 last:border-0">
                <td className="px-3 py-2 text-right text-muted-foreground">{row.rank}</td>
                <th scope="row" className="px-3 py-2 text-left font-medium">
                  {/*
                    * `prefetch={false}` matters here and nowhere else on this page.
                    *
                    * `/instruments/[symbol]` renders dynamically and calls the API. Next prefetches
                    * every `<Link>` that enters the viewport, so the default would fire eight
                    * server renders and eight API round-trips the moment the landing page paints —
                    * for links most visitors will never click, on the one page whose Lighthouse
                    * performance score is an acceptance criterion. They still prefetch on hover.
                    */}
                  <Link
                    className="hover:underline"
                    href={`/instruments/${row.symbol}`}
                    prefetch={false}
                  >
                    {row.symbol}
                  </Link>
                  <span className="block text-xs font-normal text-muted-foreground">
                    {row.name}
                  </span>
                </th>
                {row.cells.map((cell) => (
                  <td key={cell.key} className="px-3 py-2 text-right">
                    {cell.value}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <figcaption className="text-xs text-muted-foreground">
        <strong className="font-medium text-foreground">{SAMPLE_SCREEN_NAME}</strong> —{" "}
        {SAMPLE_SCREEN_UNIVERSE}, ranked by {SAMPLE_SCREEN_FACTOR}, median traded value above ₹1
        crore. {sample.resultCount.toLocaleString("en-IN")} names passed the filters on{" "}
        {formatTradeDate(sample.asOf)}; the first {sample.rows.length} are shown. This is the screen
        run itself, not a picture of one.
      </figcaption>
    </figure>
  );
}
