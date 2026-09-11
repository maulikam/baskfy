"use client";

import { useCallback, useMemo, useState, type ReactNode } from "react";
import { Eye, EyeOff } from "lucide-react";

import { AmountsProvider, useAmounts } from "@/components/portfolio/amounts";
import { ActivityTab } from "@/components/portfolio/detail/activity-tab";
import { AllocationTab } from "@/components/portfolio/detail/allocation-tab";
import { HoldingsTab } from "@/components/portfolio/detail/holdings-tab";
import { OverviewTab } from "@/components/portfolio/detail/overview-tab";
import { PerformanceTab } from "@/components/portfolio/detail/performance-tab";
import { RebalanceTab } from "@/components/portfolio/detail/rebalance-tab";
import { RiskTab } from "@/components/portfolio/detail/risk-tab";
import { SettingsTab } from "@/components/portfolio/detail/settings-tab";
import { TabPanel, TabStrip } from "@/components/portfolio/detail/tab-strip";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  allocationView,
  detailSnapshot,
  executionNote,
  holdingRows,
  holdingsContext,
  movers,
  riskView,
  settingRows,
  workspaceStates,
  type DetailTabId,
} from "@/lib/portfolio/detail-tabs";
import { isMonitoringView, valuedAtLine } from "@/lib/portfolio/detail-view";
import { MONITORING_NOTE } from "@/lib/portfolio/overview";
import type {
  ActivityItem,
  NavRange,
  NavSeries,
  PortfolioDetail,
} from "@/lib/portfolio/overview";

/**
 * PC3 — the eight-tab portfolio detail workspace, assembled.
 *
 * `docs/PORTFOLIO-COMMAND-CENTER.md` §6.1 gives this leaf `lib/portfolio/detail-tabs.ts` and this
 * directory; the parent mounts this component on `/portfolio/[id]`. The prop contract is written
 * out in `docs/pc-findings/pc3.md` and repeated on {@link PortfolioDetailWorkspaceProps}, because
 * a contract that lives only in a document is a contract somebody wires up from memory.
 *
 * ## Why tabs at all, when the page it supersedes was one scroll
 *
 * The page it supersedes showed a summary, a chart, a table and a feed at once, which is the right
 * shape for four blocks and the wrong one for eight. Holdings is a sixteen-column table, Risk is a
 * page of prose about what is not measured, and Performance is a chart plus five derived panels.
 * Stacked, that is a scroll nobody reaches the bottom of, and the two tabs that most need reading
 * are the ones furthest down it. Tabs also give each surface a name, which is what makes "the
 * allocation cannot show sectors" findable rather than something a reader stumbles into.
 *
 * ## One derivation, shared by every tab
 *
 * Every tab reads from one memoised pass over the payload. Two tabs deriving weights separately
 * is two tabs free to disagree about what a weight is, and the disagreement would show up as a
 * concentration score that does not match the column it is computed from.
 *
 * ## What this component does not do
 *
 * It fetches nothing and it writes nothing. It places no order and links to nothing that does.
 * PC4's rebalance drawer and PC6's management drawer are passed in as slots rather than imported,
 * because §6.1 says no leaf edits another leaf's file and importing one would make this leaf's
 * tests depend on a file two sessions away from existing.
 */

export interface PortfolioDetailWorkspaceProps {
  /** `bundle.detail` from `loadPortfolioDetail`. Required: with no summary there is no page. */
  detail: PortfolioDetail;
  /** `bundle.nav`. `null` is a legitimate state and is rendered as one. */
  nav: NavSeries | null;
  /** `bundle.activity`. `null` means the read failed; `[]` means nothing has happened. */
  activity: readonly ActivityItem[] | null;
  /** `bundle.failures`, minus `detail` which the parent handles by not rendering this at all. */
  failures?: {
    readonly nav?: string | null;
    readonly activity?: string | null;
  };
  /** Which tab opens first. Defaults to Overview. */
  initialTab?: DetailTabId;
  /** Fired on every tab change, for a parent that mirrors the tab into the URL. */
  onTabChange?: ((tab: DetailTabId) => void) | undefined;
  /** The chart's range pills. Omit and the pills do not render. */
  onRangeChange?: ((range: NavRange) => void) | undefined;
  chartLoading?: boolean;
  /** PC4's rebalance drawer. */
  rebalanceSlot?: ReactNode;
  onOpenRebalance?: (() => void) | undefined;
  /** PC6's management drawer. */
  manageSlot?: ReactNode;
  onManage?: (() => void) | undefined;
  /** Anything the page still owns, rendered under the tab panel. */
  children?: ReactNode;
}

export function PortfolioDetailWorkspace(props: PortfolioDetailWorkspaceProps) {
  return (
    <AmountsProvider>
      <Workspace {...props} />
    </AmountsProvider>
  );
}

function Workspace({
  detail,
  nav,
  activity,
  failures,
  initialTab = "overview",
  onTabChange,
  onRangeChange,
  chartLoading = false,
  rebalanceSlot,
  onOpenRebalance,
  manageSlot,
  onManage,
  children,
}: PortfolioDetailWorkspaceProps) {
  const [tab, setTab] = useState<DetailTabId>(initialTab);

  const select = useCallback(
    (next: DetailTabId) => {
      setTab(next);
      onTabChange?.(next);
    },
    [onTabChange],
  );

  const summary = detail.summary;
  const context = useMemo(() => holdingsContext(detail), [detail]);
  const rows = useMemo(() => holdingRows(detail), [detail]);
  const allocation = useMemo(() => allocationView(detail), [detail]);
  const snapshot = useMemo(() => detailSnapshot(detail, nav), [detail, nav]);
  const risk = useMemo(() => riskView(detail, nav), [detail, nav]);
  const rankings = useMemo(() => movers(detail), [detail]);
  const states = useMemo(() => workspaceStates(detail, nav), [detail, nav]);
  const settings = useMemo(() => settingRows(detail), [detail]);

  /* A count of 0 beside Activity when the feed failed to load would be a small lie: "nothing
     happened here" and "we could not ask" are opposite messages. The count is simply absent when
     the read failed, and the tab's own panel says which of the two it is. */
  const counts = useMemo(
    () => ({
      holdings: rows.length,
      ...(activity === null ? {} : { activity: activity.length }),
    }),
    [rows.length, activity],
  );
  const alerts = useMemo(
    () => ({
      overview: states.filter((state) => state.severity === "critical").length,
      holdings: rows.filter((row) => row.flags.length > 0).length,
    }),
    [states, rows],
  );

  const monitoring = isMonitoringView(summary);

  return (
    <div className="flex flex-col gap-4" data-testid="detail-workspace">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="truncate text-xl font-semibold">{summary.name}</h1>
          <p className="mt-0.5 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <Badge variant={monitoring ? "neutral" : "accent"}>{summary.source_badge}</Badge>
            <span>{summary.status}</span>
          </p>
        </div>
        <AmountsToggle />
      </header>

      {monitoring ? (
        <p
          data-testid="detail-monitoring-note"
          className="rounded-lg border border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground"
        >
          {summary.excluded_note ?? MONITORING_NOTE}
        </p>
      ) : null}

      <TabStrip active={tab} onSelect={select} counts={counts} alerts={alerts} />

      <TabPanel id="overview" active={tab}>
        <OverviewTab
          detail={detail}
          snapshot={snapshot}
          allocation={allocation}
          movers={rankings}
          states={states}
          activity={activity}
          activityUnavailable={failures?.activity ?? null}
          onOpenTab={select}
        />
      </TabPanel>

      <TabPanel id="holdings" active={tab}>
        <HoldingsTab
          rows={rows}
          basketBacked={context.basketBacked}
          pricedOnLine={valuedAtLine(summary)}
        />
      </TabPanel>

      <TabPanel id="performance" active={tab}>
        <PerformanceTab
          summary={summary}
          nav={nav}
          unavailableReason={failures?.nav ?? null}
          onRangeChange={onRangeChange}
          chartLoading={chartLoading}
        />
      </TabPanel>

      <TabPanel id="allocation" active={tab}>
        <AllocationTab allocation={allocation} />
      </TabPanel>

      <TabPanel id="risk" active={tab}>
        <RiskTab risk={risk} />
      </TabPanel>

      <TabPanel id="rebalance" active={tab}>
        <RebalanceTab
          basketBacked={context.basketBacked}
          basketName={detail.source_panel.basket_name ?? null}
          status={summary.status}
          rows={rows}
          executionNote={executionNote(detail)}
          rebalanceSlot={rebalanceSlot}
          onOpenRebalance={onOpenRebalance}
        />
      </TabPanel>

      <TabPanel id="activity" active={tab}>
        <ActivityTab items={activity} unavailableReason={failures?.activity ?? null} />
      </TabPanel>

      <TabPanel id="settings" active={tab}>
        <SettingsTab
          rows={settings}
          executionNote={executionNote(detail)}
          manageSlot={manageSlot}
          onManage={onManage}
        />
      </TabPanel>

      {children}
    </div>
  );
}

/** The show/hide-amounts courtesy, carried onto this workspace so the provider is not inert. */
function AmountsToggle() {
  const { visible, toggle } = useAmounts();
  return (
    <Button
      variant="secondary"
      size="sm"
      onClick={toggle}
      aria-pressed={!visible}
      data-testid="detail-amounts-toggle"
      className="rounded-full"
    >
      {visible ? <EyeOff aria-hidden="true" /> : <Eye aria-hidden="true" />}
      {visible ? "Hide amounts" : "Show amounts"}
    </Button>
  );
}
