import type { Metadata } from "next";

import { DeskNav, ReadOnlyFooter } from "@/components/desk/ui";
import { RegimeView } from "@/components/regime/regime-view";
import { PageHeader } from "@/components/shell/page-header";
import { PAGES } from "@/lib/vocabulary";

/**
 * `/regime` — a port of the desk's Regime page (`kite-momentum-rebalancer/app/templates/regime.html`),
 * read from the same payload (`GET /desk/regime/data`). Read-only: the stance is evaluated by the
 * desk on its own schedule and nothing here changes state.
 */
export const metadata: Metadata = {
  title: PAGES["/regime"].title,
  description: "How defensive the strategy is being right now, and the reasons it gave.",
};

export default function RegimePage() {
  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title={PAGES["/regime"].title}
        blurb={PAGES["/regime"].blurb}
        actions={
          <a
            href="/desk/regime/backtest"
            className="rounded-md border border-border/70 bg-card px-2.5 py-1 text-xs font-medium transition-colors duration-150 hover:bg-muted"
          >
            Regime backtest
          </a>
        }
      />
      <RegimeView />
      <DeskNav current="/regime" />
      <ReadOnlyFooter />
    </div>
  );
}
