"use client";

import { ArrowRight } from "lucide-react";

import {
  Figure,
  MetricValue,
  NotYetMeasured,
  Panel,
  ShareBar,
  StateCallout,
  TradeDate,
} from "@/components/portfolio/detail/primitives";
import { Badge } from "@/components/ui/badge";
import { formatTradeDate } from "@/lib/format";
import {
  BLOCKED_OVERVIEW,
  type AllocationView,
  type DetailSnapshot,
  type Movers,
  type WorkspaceState,
} from "@/lib/portfolio/detail-tabs";
import { syncedAtLine, valuedAtLine, type PortfolioSummary } from "@/lib/portfolio/detail-view";
import type { ActivityItem, PortfolioDetail } from "@/lib/portfolio/overview";

/**
 * The Overview tab: what this portfolio is, what it is worth, and what needs a person.
 *
 * The brief's list for this tab is long, and roughly a quarter of it does not exist. Objective,
 * last rebalance, next review and a target exposure are all absent for reasons about the data
 * rather than about the design, and {@link BLOCKED_OVERVIEW} says so at the bottom of the tab
 * rather than four blank rows saying it by implication.
 *
 * ## Order is the argument
 *
 * Alerts come before figures. A reader whose portfolio has three holdings frozen out of its
 * return series should learn that before they read the return, not after — the numbers below a
 * reconciliation problem are measured over less than everything they hold, and the ordering is
 * the only part of the layout that can say so before they are read.
 *
 * ## The model's record is never the reader's
 *
 * `headline_return` and `model_return` sit in two bordered panels with two headings and a
 * sentence on the second saying whose record it is. §11 criterion 5 forbids blending them, and a
 * border is the strongest reading of "separately" a layout can express.
 */

export interface OverviewTabProps {
  detail: PortfolioDetail;
  snapshot: DetailSnapshot;
  allocation: AllocationView;
  movers: Movers;
  states: readonly WorkspaceState[];
  activity: readonly ActivityItem[] | null;
  activityUnavailable: string | null;
  onOpenTab: (tab: "holdings" | "performance" | "allocation" | "risk" | "activity") => void;
}

export function OverviewTab({
  detail,
  snapshot,
  allocation,
  movers,
  states,
  activity,
  activityUnavailable,
  onOpenTab,
}: OverviewTabProps) {
  const summary = detail.summary;
  const recent = (activity ?? []).slice(0, 6);

  /* One element, placed in one of two slots depending on whether there is a model panel to sit
     beside. Two copies of this JSX is two panels free to drift apart. */
  const split = (
    <Panel
      title="How the money is split"
      blurb="Shares against cash, and the largest names."
      testId="overview-split"
    >
      <AllocationSummary allocation={allocation} onOpen={() => onOpenTab("allocation")} />
    </Panel>
  );

  return (
    <>
      {states.length > 0 ? (
        <Panel
          title="What needs you"
          blurb="Every one of these is a statement about the data behind this page, and each carries one next step."
          testId="overview-states"
        >
          <ul className="divide-y divide-border/60">
            {states.map((state) => (
              <StateCallout key={state.id} state={state} />
            ))}
          </ul>
        </Panel>
      ) : null}

      <Identity detail={detail} />

      <section
        aria-label="Key figures"
        data-testid="overview-snapshot"
        className="overflow-hidden rounded-xl border border-border bg-card"
      >
        <div className="flex flex-wrap divide-x divide-border border-b border-border">
          <div className="min-w-[15rem] flex-1">
            <Figure metric={snapshot.value} emphasis="hero" />
          </div>
          <div className="min-w-[15rem] flex-1">
            <Figure metric={snapshot.todaysPnl} emphasis="hero" signed />
          </div>
        </div>
        <div className="flex flex-wrap divide-x divide-border">
          <div className="min-w-[10rem] flex-1">
            <Figure metric={snapshot.invested} />
          </div>
          <div className="min-w-[10rem] flex-1">
            <Figure metric={snapshot.cash} />
          </div>
          <div className="min-w-[10rem] flex-1">
            <Figure metric={snapshot.totalPnl} signed />
          </div>
          <div className="min-w-[10rem] flex-1">
            <Figure metric={snapshot.xirr} kind="percent" signed />
          </div>
          <div className="min-w-[10rem] flex-1">
            <Figure metric={snapshot.drawdown} kind="percent" signed />
          </div>
          <div className="min-w-[10rem] flex-1">
            <Figure metric={snapshot.holdingsCount} kind="count" />
          </div>
        </div>
      </section>

      <div className="grid gap-4 lg:grid-cols-2">
        <Panel
          title="Your return, on your money"
          blurb="Two measurements of the same portfolio, each answering a different question."
          testId="overview-your-return"
        >
          <div className="divide-y divide-border/60">
            <Figure metric={snapshot.headlineReturn} kind="percent" signed emphasis="hero" />
            <Figure metric={snapshot.benchmarkReturn} kind="percent" signed />
            <Figure metric={snapshot.benchmarkGap} kind="percent" signed />
          </div>
          <p className="border-t border-border px-4 py-2.5 text-xs leading-snug text-muted-foreground">
            Your return and the index are stated separately above, so the difference between them
            is a figure you can check rather than one you have to trust.
          </p>
        </Panel>

        {summary.model_return ? (
          <Panel
            title="The published model's own record"
            blurb="Not your money, and never added to it."
            testId="overview-model-return"
            className="border-dashed bg-muted/20"
          >
            <Figure metric={snapshot.modelReturn} kind="percent" signed emphasis="hero" />
            <p className="border-t border-border/60 px-4 py-2.5 text-xs leading-snug text-muted-foreground">
              This is what {summary.publisher ?? "the publisher"} reports for the model itself. It
              is measured on the model, starting when the model started, not on your account. The
              two figures are never added together, averaged, or subtracted from one another.
            </p>
          </Panel>
        ) : (
          /* With no model to sit beside it, the return panel would be half a row of white
             space, so the split moves up to fill it rather than the row stretching. */
          split
        )}
      </div>

      {summary.model_return ? split : null}

      <div className="grid gap-4 lg:grid-cols-2">
        <Panel
          title="What has helped"
          blurb="Ranked on rupees, not on percentages: a 40 percent gain on a small position has not moved this portfolio."
          testId="overview-contributors"
        >
          <MoverList
            entries={movers.contributors}
            empty="Nothing here is worth more than it cost."
            unavailable={movers.unavailable}
            onOpen={() => onOpenTab("holdings")}
          />
        </Panel>
        <Panel
          title="What has hurt"
          blurb="The same ranking, from the other end."
          testId="overview-detractors"
        >
          <MoverList
            entries={movers.detractors}
            empty="Nothing here is worth less than it cost."
            unavailable={movers.unavailable}
            onOpen={() => onOpenTab("holdings")}
          />
        </Panel>
      </div>

      <Panel
        title="Recently here"
        blurb="The last few things that happened in this portfolio."
        testId="overview-activity"
        actions={
          <button
            type="button"
            onClick={() => onOpenTab("activity")}
            className="inline-flex items-center gap-1 text-xs font-medium text-brand-strong underline-offset-4 hover:underline"
          >
            All activity
            <ArrowRight aria-hidden="true" className="size-3" />
          </button>
        }
      >
        {activityUnavailable !== null ? (
          <p className="px-4 py-5 text-sm text-muted-foreground">{activityUnavailable}</p>
        ) : recent.length === 0 ? (
          <p className="px-4 py-5 text-sm text-muted-foreground">
            Nothing has happened in this portfolio yet. Buys, sells, cash you assign, dividends,
            corporate actions and reconciliation answers all land here.
          </p>
        ) : (
          <ul className="divide-y divide-border/60">
            {recent.map((item, index) => (
              <li
                key={`${item.on}-${item.kind}-${index}`}
                className="flex items-start justify-between gap-3 px-4 py-2.5"
              >
                <div className="min-w-0">
                  <p className="truncate text-sm">{item.description}</p>
                  <p className="text-xs text-muted-foreground">
                    <TradeDate iso={item.on} />
                    {item.broker ? ` · ${item.broker.label}` : ""}
                  </p>
                </div>
                <span className="shrink-0 text-xs text-muted-foreground">
                  {item.is_pnl_event === false ? "No profit or loss from this" : null}
                </span>
              </li>
            ))}
          </ul>
        )}
      </Panel>

      <NotYetMeasured
        items={BLOCKED_OVERVIEW}
        heading="What an overview would also say, once the record carries it"
        intro="The brief asks this tab for four facts that no field in Baskfy holds. They are named here rather than left as four blank rows."
        testId="overview-blocked"
      />
    </>
  );
}

/** Who this portfolio is: the facts that do not change between one visit and the next. */
function Identity({ detail }: { detail: PortfolioDetail }) {
  const summary: PortfolioSummary = detail.summary;
  const panel = detail.source_panel;
  const brokers = detail.brokers ?? [];

  return (
    <Panel
      title="What this portfolio is"
      blurb={panel.headline}
      testId="overview-identity"
      actions={
        <Badge variant={summary.counts_toward_total ? "accent" : "neutral"}>
          {summary.counts_toward_total ? "Counts toward your net worth" : "Monitoring view"}
        </Badge>
      }
    >
      <dl className="grid gap-x-6 gap-y-3 px-4 py-3 text-sm sm:grid-cols-2 lg:grid-cols-4">
        <Fact label="Strategy type">{summary.source_badge}</Fact>
        <Fact label="Broker accounts">
          {brokers.length === 0
            ? "None attached to this portfolio"
            : brokers.map((broker) => broker.label).join(", ")}
        </Fact>
        <Fact label="Created">{formatTradeDate(summary.started_on)}</Fact>
        <Fact label="Benchmark">
          {summary.benchmark?.name ?? "None chosen for this portfolio"}
        </Fact>
        <Fact label="Status">{summary.status}</Fact>
        <Fact label="Last rebalanced">
          Not recorded. The activity feed has no rebalance event to date.
        </Fact>
        <Fact label="Next review">Not recorded. No review cadence is stored.</Fact>
        <Fact label="Objective">Not recorded. A portfolio has no objective field yet.</Fact>
      </dl>
      <p data-testid="overview-clocks" className="border-t border-border px-4 py-2.5 text-xs text-muted-foreground">
        {valuedAtLine(summary)} · {syncedAtLine(summary)}. Those are two different clocks and are
        kept apart on purpose: a fresh sync should never vouch for an old price.
      </p>
      <p className="border-t border-border px-4 py-2.5 text-xs text-muted-foreground">
        {panel.execution_note}
      </p>
    </Panel>
  );
}

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-[0.6875rem] font-medium uppercase tracking-wide text-muted-foreground">
        {label}
      </dt>
      <dd className="mt-0.5 text-sm">{children}</dd>
    </div>
  );
}

function AllocationSummary({
  allocation,
  onOpen,
}: {
  allocation: AllocationView;
  onOpen: () => void;
}) {
  return (
    <>
      <div className="flex flex-wrap divide-x divide-border border-b border-border">
        <div className="min-w-[9rem] flex-1">
          <Figure metric={allocation.deployedShare} kind="percent" />
        </div>
        <div className="min-w-[9rem] flex-1">
          <Figure metric={allocation.cashShare} kind="percent" />
        </div>
        <div className="min-w-[9rem] flex-1">
          <Figure metric={allocation.top3} kind="percent" />
        </div>
      </div>
      <ul className="divide-y divide-border/60">
        {allocation.bySecurity.slice(0, 5).map((bucket) => (
          <li key={bucket.key} className="flex items-center justify-between gap-3 px-4 py-2">
            <span className="min-w-0">
              <span className="block truncate text-sm font-medium">{bucket.label}</span>
              {bucket.secondary ? (
                <span className="block truncate text-xs text-muted-foreground">
                  {bucket.secondary}
                </span>
              ) : null}
            </span>
            <ShareBar metric={bucket.weight} />
          </li>
        ))}
      </ul>
      <div className="border-t border-border px-4 py-2.5">
        <button
          type="button"
          onClick={onOpen}
          className="inline-flex items-center gap-1 text-xs font-medium text-brand-strong underline-offset-4 hover:underline"
        >
          The whole allocation, and what it cannot show
          <ArrowRight aria-hidden="true" className="size-3" />
        </button>
      </div>
    </>
  );
}

function MoverList({
  entries,
  empty,
  unavailable,
  onOpen,
}: {
  entries: Movers["contributors"];
  empty: string;
  unavailable: string | null;
  onOpen: () => void;
}) {
  if (unavailable !== null) {
    return <p className="px-4 py-5 text-sm text-muted-foreground">{unavailable}</p>;
  }
  if (entries.length === 0) {
    return <p className="px-4 py-5 text-sm text-muted-foreground">{empty}</p>;
  }
  return (
    <>
      <ul className="divide-y divide-border/60">
        {entries.map((entry) => (
          <li
            key={entry.row.key}
            data-testid={`mover-${entry.row.symbol}`}
            className="flex items-center justify-between gap-3 px-4 py-2"
          >
            <span className="min-w-0">
              <span className="block truncate text-sm font-medium">{entry.row.symbol}</span>
              <span className="block truncate text-xs text-muted-foreground">{entry.row.name}</span>
            </span>
            <span className="shrink-0 text-right">
              <MetricValue metric={entry.figure} kind="rupees" signed className="text-sm font-semibold" />
              <span className="mt-0.5 block text-xs">
                <MetricValue
                  metric={entry.row.unrealisedPct}
                  kind="percent"
                  signed
                  compact={entry.row.unrealisedPct.value === null}
                  short="no purchase price"
                />
              </span>
            </span>
          </li>
        ))}
      </ul>
      <div className="border-t border-border px-4 py-2.5">
        <button
          type="button"
          onClick={onOpen}
          className="inline-flex items-center gap-1 text-xs font-medium text-brand-strong underline-offset-4 hover:underline"
        >
          Every holding, with all sixteen figures
          <ArrowRight aria-hidden="true" className="size-3" />
        </button>
      </div>
    </>
  );
}
