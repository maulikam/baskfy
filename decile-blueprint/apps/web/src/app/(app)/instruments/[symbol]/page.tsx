import type { Metadata } from "next";
import { notFound } from "next/navigation";

import type { FactsheetOut } from "@baskfy/api-client";

import { Factsheet } from "@/components/instrument/factsheet";
import { FactsheetNotFound, fetchFactsheet, fetchHistory } from "@/lib/instrument/fetch";
import { instrumentJsonLd, instrumentMetadata, SPARKLINE_FIELDS } from "@/lib/instrument/seo";

/**
 * `/instruments/[symbol]` — docs/08 §Routes: "**ISR** · SEO-optimised (this is the
 * organic-traffic surface)".
 *
 * ISR, not SSR: a factsheet changes once a night, and re-rendering it per request would put a
 * database round-trip in front of every crawler visit for a page whose content is identical
 * between publishes. `revalidate` here is the time-based backstop; the real trigger is
 * `revalidateTag(FACTSHEET_TAG)` from `POST /api/revalidate`, which the nightly publish step
 * calls when `data_version` moves. See `src/lib/instrument/fetch.ts`.
 *
 * `dynamicParams` is left at its default, so any symbol renders on first request and is cached
 * from then on — pre-generating 2,300 pages at build time would be a long build for pages most of
 * which nobody will ask for that day.
 */
export const revalidate = 3600;

export async function generateMetadata({
  params,
}: {
  params: Promise<{ symbol: string }>;
}): Promise<Metadata> {
  const { symbol } = await params;
  try {
    const sheet = await fetchFactsheet(symbol);
    return instrumentMetadata(sheet);
  } catch {
    // A 404 body still needs a title, and `notFound()` in the page below is what actually
    // produces the 404 status. Returning a generic title here beats crashing metadata generation.
    return { title: symbol.toUpperCase(), robots: { index: false, follow: false } };
  }
}

async function sparklineSeries(symbol: string): Promise<Record<string, readonly number[]>> {
  const results = await Promise.all(
    SPARKLINE_FIELDS.map(async (field) => {
      const history = await fetchHistory(symbol, field);
      const values = (history?.points ?? [])
        .map((point) => (point.value === null || point.value === undefined ? NaN : Number(point.value)))
        .filter((value) => !Number.isNaN(value));
      return [field, values] as const;
    }),
  );
  return Object.fromEntries(results);
}

export default async function InstrumentPage({
  params,
}: {
  params: Promise<{ symbol: string }>;
}) {
  const { symbol } = await params;

  let sheet: FactsheetOut;
  try {
    sheet = await fetchFactsheet(symbol);
  } catch (error) {
    if (error instanceof FactsheetNotFound) notFound();
    throw error;
  }

  const series = await sparklineSeries(sheet.symbol);

  return (
    <>
      {/*
        JSON-LD, server-rendered — docs/08 §"Instrument factsheet" asks for it by name. It is a
        `<script type="application/ld+json">`, which React renders verbatim; the payload is built
        from the same object the page renders, so the two cannot drift.
      */}
      <script
        type="application/ld+json"
        // JSON-LD has no non-`dangerously` form. The payload is `JSON.stringify` of an object we
        // built ourselves from the API response — never a raw string, and never user input.
        dangerouslySetInnerHTML={{ __html: JSON.stringify(instrumentJsonLd(sheet)) }}
      />
      <Factsheet sheet={sheet} series={series} />
    </>
  );
}
