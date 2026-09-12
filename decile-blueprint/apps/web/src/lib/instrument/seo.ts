import type { Metadata } from "next";

import type { FactsheetOut } from "@baskfy/api-client";

import { formatTradeDate } from "@/lib/format";
import { SITE_NAME, SITE_URL } from "@/lib/site";

/**
 * SEO for `/instruments/[symbol]` — docs/08 §"Instrument factsheet":
 *
 *     "SEO: server-rendered title `CUPID share price, momentum & factor analysis`, JSON-LD, OG
 *      image generated per instrument."
 *
 * The title template is docs/08's, verbatim, with the symbol substituted. Everything else here
 * exists to make that title honest: the description states the as-of date and the actual numbers,
 * so a search result that promises factor analysis delivers it, and the JSON-LD describes the
 * page as what it is — a dated dataset derived from published market data, not advice.
 */

/** The five metric cards' series, fetched for the sparklines. docs/01 §5 block 4. */
export const SPARKLINE_FIELDS = ["close", "ret_12m", "pe", "marketcap_cr", "rsi_12m"] as const;

export function instrumentTitle(symbol: string): string {
  return `${symbol} share price, momentum & factor analysis`;
}

export function instrumentPath(symbol: string): string {
  return `/instruments/${encodeURIComponent(symbol)}`;
}

function cellText(sheet: FactsheetOut, block: "returns" | "volatility", label: string): string {
  const cell = sheet[block].find((item) => item.label === label);
  if (!cell || cell.value === null || cell.value === undefined) return "not yet available";
  return String(cell.value);
}

export function instrumentDescription(sheet: FactsheetOut): string {
  const oneYear = cellText(sheet, "returns", "1Y");
  const universes = sheet.index_memberships.map((index) => index.name).slice(0, 2);
  const membership = universes.length > 0 ? ` Member of ${universes.join(" and ")}.` : "";
  return (
    `${sheet.header.name} (${sheet.header.exchange}: ${sheet.symbol}) factor snapshot for ` +
    `${formatTradeDate(sheet.as_of)}: 1-year return ${oneYear}%, with Sharpe, volatility, RSI, ` +
    `moving averages and corporate actions.${membership} Not investment advice.`
  );
}

export function instrumentMetadata(sheet: FactsheetOut): Metadata {
  const title = instrumentTitle(sheet.symbol);
  const description = instrumentDescription(sheet);
  const path = instrumentPath(sheet.symbol);
  return {
    title,
    description,
    alternates: { canonical: path },
    robots: { index: false, follow: false },
    openGraph: {
      type: "article",
      title,
      description,
      url: `${SITE_URL}${path}`,
      siteName: SITE_NAME,
      locale: "en_IN",
    },
    twitter: { card: "summary_large_image", title, description },
  };
}

/** A JSON-LD value. `unknown` rather than `any` — the web app forbids `any` outright. */
export type JsonLd = Record<string, unknown>;

/**
 * `Dataset` plus `BreadcrumbList`.
 *
 * `Dataset` rather than a financial-product type because that is what the page is: a dated,
 * derived measurement of a security, free to read, with named variables. Claiming
 * `FinancialProduct` would imply we are offering one, which docs/11 §"Compliance & legal (India)"
 * is precisely about not doing.
 */
export function instrumentJsonLd(sheet: FactsheetOut): JsonLd {
  const path = instrumentPath(sheet.symbol);
  const url = `${SITE_URL}${path}`;
  const variables = [
    ...sheet.returns,
    ...sheet.sharpe_returns,
    ...sheet.volatility,
    ...sheet.rsi,
  ].map((cell) => `${cell.key}`);

  return {
    "@context": "https://schema.org",
    "@graph": [
      {
        "@type": "BreadcrumbList",
        // Two levels, not three. An "Instruments" crumb would point at the listings index,
        // which Prompt 11 delivers; a breadcrumb to a 404 is worse than a shallow one.
        itemListElement: [
          { "@type": "ListItem", position: 1, name: SITE_NAME, item: SITE_URL },
          { "@type": "ListItem", position: 2, name: sheet.symbol, item: url },
        ],
      },
      {
        "@type": "Dataset",
        "@id": url,
        name: instrumentTitle(sheet.symbol),
        description: instrumentDescription(sheet),
        url,
        isAccessibleForFree: true,
        temporalCoverage: sheet.as_of,
        dateModified: sheet.as_of,
        creator: { "@type": "Organization", name: SITE_NAME, url: SITE_URL },
        license: `${SITE_URL}/terms`,
        variableMeasured: variables,
        about: {
          "@type": "Corporation",
          name: sheet.header.name,
          tickerSymbol: `${sheet.header.exchange}:${sheet.symbol}`,
          ...(sheet.header.isin ? { identifier: sheet.header.isin } : {}),
        },
      },
    ],
  };
}
