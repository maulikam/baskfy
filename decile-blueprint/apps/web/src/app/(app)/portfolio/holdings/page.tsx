import type { Metadata } from "next";

import { createPortfolioAction } from "@/app/actions/portfolio";
import { UnallocatedSection } from "@/components/portfolio/unallocated-section";
import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { fetchBrokerCatalog } from "@/lib/brokers/fetch";
import {
  fetchGroupingSuggestions,
  fetchPortfolioHoldings,
  fetchPortfolioOverview,
} from "@/lib/portfolio/fetch";
import { isUnallocated, type AggregatedHolding } from "@/lib/portfolio/organize";
import { formatRupees } from "@/lib/portfolios/decimal";
import { PAGES } from "@/lib/vocabulary";

/**
 * `/portfolio/holdings` — PORTFOLIO_REDESIGN.md §2: "the flat, broker-level truth".
 *
 * §6.6 puts the Unallocated section at the top of it as a **centrepiece, not a footer**, and this
 * page is where the §6.7 grouping flow lives. Audit 1.6: the page must also list every holding
 * grouped by portfolio — allocated shares were invisible while the subtitle promised every share.
 */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/portfolio/holdings"].title,
  description: PAGES["/portfolio/holdings"].blurb,
  robots: { index: false, follow: false },
};

function groupByPortfolio(rows: readonly AggregatedHolding[]): {
  name: string;
  rows: AggregatedHolding[];
}[] {
  const groups = new Map<string, AggregatedHolding[]>();
  for (const row of rows) {
    if (isUnallocated(row)) continue;
    const name = row.allocation?.name ?? (row.split_across_portfolios ? "Split across portfolios" : "Allocated");
    const list = groups.get(name) ?? [];
    list.push(row);
    groups.set(name, list);
  }
  return [...groups.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([name, grouped]) => ({ name, rows: grouped }));
}

export default async function PortfolioHoldingsPage() {
  const [overview, holdings, suggestions, brokers] = await Promise.all([
    fetchPortfolioOverview(),
    fetchPortfolioHoldings(),
    fetchGroupingSuggestions(),
    fetchBrokerCatalog().catch(() => null),
  ]);

  const rows = holdings?.rows ?? [];
  const connected = (brokers?.brokers ?? []).filter((broker) => broker.connected).length;
  const byPortfolio = groupByPortfolio(rows);
  const syncSummary =
    overview?.sync_summary ?? overview?.holdings_synced_label ?? holdings?.holdings_synced_label;

  return (
    <div className="flex max-w-5xl flex-col gap-6">
      <SectionTabs section="portfolio" />
      <PageHeader title="Holdings" blurb={PAGES["/portfolio/holdings"].blurb} />

      <UnallocatedSection
        unallocated={overview?.unallocated ?? null}
        rows={rows}
        suggestions={suggestions.suggestions}
        suggestionsUnavailableReason={suggestions.unavailableReason}
        sectors={suggestions.sectors}
        connectedBrokerCount={connected}
        targets={[...(overview?.portfolios ?? []), ...(overview?.monitoring_views ?? [])].map(
          (row) => ({ portfolio_id: row.portfolio_id, name: row.name, kind: row.kind }),
        )}
        onCreate={createPortfolioAction}
      />

      {byPortfolio.length > 0 ? (
        <section aria-label="Holdings by portfolio" data-testid="holdings-by-portfolio" className="space-y-4">
          <h2 className="text-base font-semibold">By portfolio</h2>
          {byPortfolio.map((group) => (
            <div key={group.name} className="rounded-xl border border-border bg-card p-4">
              <h3 className="text-sm font-medium">{group.name}</h3>
              <ul className="mt-2 divide-y divide-border">
                {group.rows.map((row) => (
                  <li
                    key={row.instrument.instrument_id}
                    className="flex flex-wrap items-baseline justify-between gap-2 py-2 text-sm"
                  >
                    <span>
                      {row.instrument.symbol}
                      <span className="ml-2 text-muted-foreground">{row.quantity} sh</span>
                    </span>
                    <span className="tabular-nums text-muted-foreground">
                      {row.value != null ? formatRupees(row.value) : "—"}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </section>
      ) : null}

      {holdings ? (
        <p className="text-xs text-muted-foreground" data-testid="sync-summary">
          {holdings.prices_label} · {syncSummary ?? holdings.holdings_synced_label}
        </p>
      ) : null}

      <p className="text-xs text-muted-foreground">
        Read-only. Shares stay in your demat account; nothing on this page places an order.
      </p>
    </div>
  );
}
