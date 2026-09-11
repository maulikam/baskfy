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
import { Button } from "@/components/ui/button";
import { allocationAnalytics } from "@/lib/portfolio/analytics";
import {
  commandCenter,
  viewsNotice,
  type CommandMode,
} from "@/lib/portfolio/command-center";
import type { Unallocated } from "@/lib/portfolio/organize";
import type { Overview } from "@/lib/portfolio/overview";

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
  onAddPortfolio?: (() => void) | undefined;
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
}: CommandCenterScreenProps) {
  const [mode, setMode] = useState<CommandMode>("capital");

  const centre = useMemo(() => commandCenter(overview, unallocated), [overview, unallocated]);

  /* Views get their own weights, computed against the views themselves — NEVER against capital.
     A lens's share of net worth is a meaningless number because the lens is not part of net
     worth, and printing one would be the exact mixing the brief forbids. */
  const viewAnalytics = useMemo(
    () => allocationAnalytics(centre?.views ?? [], null),
    [centre?.views],
  );

  if (error) {
    return (
      <Shell>
        <Explain
          title="Your portfolios could not be loaded"
          body={`${error} Nothing is wrong with your holdings — this screen could not reach the service that reads them.`}
          action={{ label: "Try again", href: "/portfolio/portfolios" as Route }}
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
          action={{ label: "Connect a broker", href: "/portfolio/holdings" as Route }}
        />
      </Shell>
    );
  }

  const counts = { capital: centre.capital.length, views: centre.views.length } as const;
  const showingViews = mode === "views";
  const attention = itemsFromProblems(centre.health.problems);

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
      />

      <HealthStrip health={centre.health} />

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
              action={{ label: "Create one", href: "/portfolio/holdings" as Route }}
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
                  action={{ label: "Organise holdings", href: "/portfolio/holdings" as Route }}
                />
              ) : (
                <ComparisonTable
                  slices={centre.allocation.slices}
                  rows={centre.capital}
                  mode="capital"
                />
              )}

              {/* The chart's honest seam. PC2 builds it; a placeholder rectangle here would be a
                  promise the screen cannot keep. */}
              <p className="text-xs text-muted-foreground">
                Value over time, the benchmark and the drawdown are on{" "}
                <Link
                  href="/portfolio/overview"
                  className="font-medium text-brand-strong underline-offset-4 hover:underline"
                >
                  the overview chart
                </Link>
                .
              </p>
            </div>

            <AttentionRail
              items={attention}
              className="order-1 self-start xl:order-2 xl:sticky xl:top-4"
            />
          </div>
        </>
      )}
    </Shell>
  );
}
