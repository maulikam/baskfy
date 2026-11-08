import type { Metadata } from "next";
import { notFound } from "next/navigation";

import type { FactsheetOut } from "@baskfy/api-client";

import { Factsheet } from "@/components/instrument/factsheet";
import { FactsheetNotFound, fetchFactsheet, fetchHistory } from "@/lib/instrument/fetch";
import { instrumentJsonLd, instrumentMetadata, SPARKLINE_FIELDS } from "@/lib/instrument/seo";

/**
 * `/instruments/[symbol]` — docs/08 §Routes: SEO-optimised factsheet surface.
 *
 * Page-level `revalidate` is inert under `(app)/layout` (reads cookies) — AUDIT 4.7. Cache
 * invalidation is `revalidateTag(FACTSHEET_TAG)` from `POST /api/revalidate` on publish.
 * See `src/lib/instrument/fetch.ts`.
 */

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
