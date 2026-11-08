"use client";

import type { Route } from "next";
import { useMemo, useState } from "react";
import { CircleAlert, Layers } from "lucide-react";
import Link from "next/link";

import { AttentionRail, itemsFromProblems } from "@/components/portfolio/command/attention-rail";
import { CommandHeader } from "@/components/portfolio/command/command-header";
import { ComparisonTable } from "@/components/portfolio/command/comparison-table";
import { HealthStrip } from "@/components/portfolio/command/health-strip";
import { MetricBand } from "@/components/portfolio/command/metric-band";
import { PerformanceWorkspace } from "@/components/portfolio/command/performance-workspace";
import { RegimePanel } from "@/components/portfolio/command/regime-panel";
import {
  ManagePortfoliosDrawer,
  type ManageHandlers,
} from "@/components/portfolio/manage/manage-drawer";
import { Button } from "@/components/ui/button";
import { allocationAnalytics } from "@/lib/portfolio/analytics";
import {
  commandCenter,
  commandCenterCsv,
  viewsNotice,
  type CommandMode,
} from "@/lib/portfolio/command-center";
import { brokersIn, type AggregatedHolding, type Unallocated } from "@/lib/portfolio/organize";
import type { Overview } from "@/lib/portfolio/overview";
import type { RegimeOut } from "@/lib/portfolio/regime";
import { downloadCsv } from "@/lib/portfolios/export";

/**
 * The Portfolio Command Center, assembled.
 *
 * Layout is the brief's: a compact header, the operational strip, then a workspace that is a
 * two-column grid on desktop — content and the intelligence rail — and a single column below
 * `xl`, where the rail moves under the table rather than squeezing it. Brief: *"Collapse the
 * intelligence rail into a slide-over panel"* on tablet; here it collapses into flow, which
 * achieves the same thing without a second interaction model to learn.
 *
 * WHAT THIS SCREEN DOES NOT DO
 * ----------------------------
 * It places no orders and links to nothing that does. The primary action is "Review rebalance",
 * which prepares a plan for a human to check — this product's first non-negotiable, and the
 * brief's own instruction that order placement must not be the primary action.
 *
 * It also draws no chart. The brief asks for one as the central visual and the data exists
 * (`overview.chart` carries the value series, the invested line, the benchmark and the drawdown),
 * but the chart is PC2 in `docs/PORTFOLIO-COMMAND-CENTER.md` — this leaf is the command surface.
 * The link below is the honest seam rather than a placeholder rectangle.
 */

export interface CommandCenterScreenProps {
  overview: Overview | null;
  unallocated: Unallocated | null;
  /** Set when the overview call failed, so the screen can say what happened rather than look empty. */
  error?: string | null | undefined;
  /**
   * `"restricted"` when the API refused for permission rather than failing.
   *
   * The two need different next actions, and offering the wrong one is worse than offering none:
   * "Try again" to somebody who has lost access is a loop they cannot get out of.
   */
  failure?: "restricted" | "unreachable" | null | undefined;
  /**
   * Whether the exchange is open right now, so the screen can say whether today's figures are
   * still moving. Supplied by the caller because the module takes no clock.
   */
  marketOpen?: boolean | undefined;
  onAddPortfolio?: (() => void) | undefined;
  /** `GET /api/v1/desk/regime`. `null` when the desk did not answer — never an assumed tier. */
  regime?: RegimeOut | null | undefined;
  /** Today in EXCHANGE time, `YYYY-MM-DD`. `null` is honest: the panel then says the overdue
   *  check could not be made rather than treating an old evaluation as fresh. */
  regimeToday?: string | null | undefined;
  /** Why the regime is absent, when it is. */
  regimeError?: string | null | undefined;
  /** `HoldingsOut.holdings` — what the management drawer files, moves and watches. */
  holdings?: readonly AggregatedHolding[] | undefined;
  /**
   * The management drawer's writes, supplied by the PAGE rather than imported here.
   *
   * They are server actions, and this is a client component: importing them would pull `auth()`
   * and `server-only` into the browser bundle and into every test that renders this screen. The
   * page hands them down, which is the same shape `new-portfolio-flow` has always had. A handler
   * that is absent disables its control beside its reason — never a button that swallows a click.
   */
  manageHandlers?: ManageHandlers | undefined;
}

function Shell({ children }: { children: React.ReactNode }) {
  return <div className="space-y-4">{children}</div>;
}

/** Every failure and empty state says what happened and offers exactly one next step. */
function Explain({
  title,
  body,
  action,
}: {
  title: string;
  body: string;
  action?: { label: string; href: Route } | undefined;
}) {
  return (
    <section
      data-testid="command-center-explain"
      className="rounded-xl border border-dashed border-border bg-card px-5 py-8 text-center"
    >
      <CircleAlert aria-hidden="true" className="mx-auto size-5 text-muted-foreground" />
      <h2 className="mt-2 text-sm font-semibold">{title}</h2>
      <p className="mx-auto mt-1 max-w-md text-sm text-muted-foreground">{body}</p>
      {action ? (
        <Button asChild variant="outline" size="sm" className="mt-3">
          <Link href={action.href}>{action.label}</Link>
        </Button>
      ) : null}
    </section>
  );
}

export function CommandCenterScreen({
  overview,
  unallocated,
  error,
  onAddPortfolio,
  regime = null,
  regimeToday = null,
  regimeError = null,
  failure = null,
  marketOpen,
  holdings = [],
  manageHandlers,
}: CommandCenterScreenProps) {
  const [mode, setMode] = useState<CommandMode>("capital");
  const [manageOpen, setManageOpen] = useState(false);

  const centre = useMemo(() => commandCenter(overview, unallocated), [overview, unallocated]);

  /* Views get their own weights, computed against the views themselves — NEVER against capital.
     A lens's share of net worth is a meaningless number because the lens is not part of net
     worth, and printing one would be the exact mixing the brief forbids. */
  const viewAnalytics = useMemo(
    () => allocationAnalytics(centre?.views ?? [], null),
    [centre?.views],
  );

  if (failure === "restricted") {
    /* A different sentence and a different action from an outage. Nothing is broken and retrying
       changes nothing, so the next step is the only one that can actually resolve it. */
    return (
      <Shell>
        <Explain
          title="You do not have access to these portfolios"
          body="The portfolio service refused this request. Your holdings are unaffected; this session is not permitted to read them. If you expect access, ask the account owner to grant it, or sign in again with the account that owns the portfolios."
          action={{ label: "Check the signed-in account", href: "/profile" }}
        />
      </Shell>
    );
  }

  if (error) {
    return (
      <Shell>
        <Explain
          title="Your portfolios could not be loaded"
          body={`${error} Nothing is wrong with your holdings — this screen could not reach the service that reads them.`}
          action={{ label: "Try again", href: "/portfolio/portfolios" }}
        />
      </Shell>
    );
  }

  if (centre === null) {
    return (
      <Shell>
        <Explain
          title="No portfolio data yet"
          body="Connect a broker and sync your holdings, and this screen will fill in with what you own."
          action={{ label: "Connect a broker", href: "/portfolio/holdings" }}
        />
      </Shell>
    );
  }

  const counts = { capital: centre.capital.length, views: centre.views.length } as const;
  const showingViews = mode === "views";
  const attention = itemsFromProblems(centre.health.problems);

  /* The export follows the MODE, because "current view" is the brief's own word for it. Exporting
     capital rows while a person is reading views would hand them a file that does not match the
     screen they asked to export. */
  const exportSlices = showingViews ? viewAnalytics.slices : centre.allocation.slices;
  const exportName = showingViews ? "baskfy-monitoring-views" : "baskfy-capital-portfolios";
  const canExport = exportSlices.length > 0;

  return (
    <Shell>
      <CommandHeader
        mode={mode}
        onModeChange={setMode}
        counts={counts}
        onAddPortfolio={onAddPortfolio}
        rebalanceTargets={centre.capital.map((p) => ({
          portfolioId: p.portfolio_id,
          name: p.name,
        }))}
        rebalanceDisabledReason={
          centre.capital.length === 0
            ? "A rebalance compares what you hold against a target. Create a capital portfolio first."
            : undefined
        }
        portfolios={centre.capital.map((p) => ({
          portfolioId: p.portfolio_id,
          name: p.name,
        }))}
        selectedPortfolioId={null}
        onExportCsv={
          canExport
            ? () => downloadCsv(`${exportName}.csv`, commandCenterCsv(exportSlices, mode))
            : undefined
        }
        onOpenSettings={() => setManageOpen(true)}
        benchmarkName={overview?.chart?.benchmark?.name ?? null}
        exportDisabledReason={
          showingViews
            ? "There are no monitoring views to export yet."
            : "Nothing is filed into a capital portfolio yet, so there are no rows to export."
        }
      />

      <HealthStrip health={centre.health} />

      {/* Which clock the figures are on (audit 4.1). Close is the base; live overlay applies when
          a Kite session exists. Baskets/factors stay end-of-day. Rendered only when the caller
          supplies whether the market is open — a guessed session is worse than no label. */}
      {marketOpen === undefined ? null : (
        <p data-testid="market-session" className="text-xs text-muted-foreground">
          {marketOpen
            ? "Market open. Marks are close, plus live overlay when a Kite session exists — today's P&L moves with the overlay until the close."
            : "Market closed. Marks are the session close (plus any final quote overlay); today's P&L will not change again until the next session opens."}
        </p>
      )}

      {showingViews ? (
        <>
          {/* Stated every time views are on screen, in the server's own words. */}
          <p
            data-testid="views-notice"
            className="flex items-start gap-2 rounded-xl border border-info/30 bg-info-muted px-4 py-2.5 text-xs leading-relaxed text-foreground"
          >
            <Layers aria-hidden="true" className="mt-0.5 size-3.5 shrink-0 text-info" />
            {viewsNotice(overview)}
          </p>

          {centre.views.length === 0 ? (
            <Explain
              title="No monitoring views yet"
              body="A view is a lens over holdings you already own — all your small caps, everything near a stop. It never changes what a portfolio owns."
              action={{ label: "Create one", href: "/portfolio/holdings" }}
            />
          ) : (
            <ComparisonTable slices={viewAnalytics.slices} rows={centre.views} mode="views" />
          )}
        </>
      ) : (
        <>
          <MetricBand snapshot={centre.snapshot} />

          {/* Below `xl` the rail moves into flow, and `order` puts it ABOVE the table: the brief
              asks for net worth, today's move and alerts first on a phone, and a person who has
              opened this screen on one is checking whether anything needs them, not sorting a
              ten-column comparison. The explicit `xl:order-*` pair restores reading order on
              desktop, where the two sit side by side. */}
          <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_20rem]">
            <div className="order-2 min-w-0 space-y-4 xl:order-1">
              {centre.capital.length === 0 ? (
                <Explain
                  title="Nothing is filed into a portfolio yet"
                  body="Your holdings are all unallocated. Grouping them is what turns a pile of shares into something you can measure."
                  action={{ label: "Organise holdings", href: "/portfolio/holdings" }}
                />
              ) : (
                <ComparisonTable
                  slices={centre.allocation.slices}
                  rows={centre.capital}
                  mode="capital"
                />
              )}

              {/* PC2. The seam that used to be a sentence pointing at another page. */}
              <PerformanceWorkspace
                chart={overview?.chart ?? null}
                rows={centre.capital}
                todaysTotal={overview?.hero?.todays_pnl?.amount ?? null}
                todaysUnavailable={overview?.hero?.todays_pnl?.unavailable_reason ?? null}
                unallocatedValue={unallocated?.holdings_value ?? null}
                /* `null` from the server means "nothing was traded, so there are no charges";
                   `undefined` would mean this screen does not supply it. They render differently
                   and neither renders ₹0. */
                estimatedCosts={overview?.estimated_costs ?? null}
              />
            </div>

            {/* The rail is two panels, not one: what the market is doing to the exposure, then
                what needs a person. Both answer "is anything wrong", which is why they sit
                together and why they come FIRST on a phone — `order-1` below `xl`. Sorting a
                ten-column comparison is not what a person opens this screen on a phone to do. */}
            <div className="order-1 space-y-4 self-start xl:order-2 xl:sticky xl:top-4">
              <RegimePanel
                regime={regime}
                today={regimeToday}
                unavailableReason={regimeError}
              />
              <AttentionRail items={attention} />
            </div>
          </div>
        </>
      )}

      {/* PC6. Configuration lives in a drawer, off the analytical dashboard — the brief's own
          instruction. Every write is a server action: the bearer token is in the server session
          and handing it to the browser would put it where the page's scripts can read it. A
          handler this page did not pass disables its control beside the reason, which is why the
          seven are wired rather than left to default. */}
      <ManagePortfoliosDrawer
        open={manageOpen}
        onOpenChange={setManageOpen}
        capital={centre.capital}
        views={centre.views}
        rows={holdings}
        brokers={brokersIn(holdings)}
        openReconciliationCount={overview?.open_reconciliation_count ?? 0}
        handlers={manageHandlers}
      />
    </Shell>
  );
}
