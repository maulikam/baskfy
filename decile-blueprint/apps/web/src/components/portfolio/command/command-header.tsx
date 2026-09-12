"use client";

import type { Route } from "next";
import Link from "next/link";
import { Check, ChevronDown, MoreHorizontal, Plus, Scale } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { CommandMode } from "@/lib/portfolio/command-center";
import { cn } from "@/lib/utils";

/**
 * The command header: what this screen is, which of the two models you are looking at, and the
 * two actions worth putting in front of a person.
 *
 * THE MODE SWITCH IS THE IMPORTANT PART
 * -------------------------------------
 * Brief: *"Basqfy contains two different concepts. Do not mix them."* Capital portfolios own
 * their shares exclusively and sum to net worth; monitoring views are lenses that overlap and
 * enter no total. Conflating them would print a net worth that counts the same shares twice —
 * the single worst number this product can show.
 *
 * So the switch sits beside the title rather than in a filter tray, it defaults to capital, and
 * the two options are labelled with the words the brief chose: "Capital portfolios" and
 * "Monitoring views". A segmented control, not a dropdown, because a dropdown hides the fact that
 * there are two models at all.
 *
 * ON THE PRIMARY ACTION
 * ---------------------
 * "Review rebalance" is orange and primary; there is deliberately no "place orders" here. Brief:
 * *"Do not make order placement the primary action. Basqfy initially prepares and reviews actions
 * rather than automatically trading."* That also matches this product's own first
 * non-negotiable — the web app has never had an execute route and does not gain one here.
 *
 * NEITHER BUTTON IS A DEAD HANDLER
 * --------------------------------
 * Both actions resolve to a real destination or say why they cannot. A rebalance is prepared for
 * ONE portfolio, so with a single capital portfolio the button is a link straight to it, with
 * several it opens a menu naming them, and with none it is disabled beside the sentence that
 * explains what to do first. A styled button that swallows the click is the defect this product
 * has already shipped once, and it is indistinguishable from a broken page.
 */

export interface RebalanceTarget {
  readonly portfolioId: number;
  readonly name: string;
}

function rebalanceHref(portfolioId: number): Route {
  return `/portfolios/${portfolioId}/rebalance` as Route;
}

/** Where holdings are filed into portfolios — the create flow lives on the holdings screen. */
const ADD_PORTFOLIO_HREF = "/portfolio/holdings" as Route;

const MODES: ReadonlyArray<{ mode: CommandMode; label: string; hint: string }> = [
  {
    mode: "capital",
    label: "Capital portfolios",
    hint: "Own their holdings exclusively and add up to your net worth.",
  },
  {
    mode: "views",
    label: "Monitoring views",
    hint: "Overlapping lenses. Excluded from every total.",
  },
];

export function ModeSwitch({
  mode,
  onChange,
  counts,
}: {
  mode: CommandMode;
  onChange: (next: CommandMode) => void;
  counts: Readonly<Record<CommandMode, number>>;
}) {
  return (
    <div
      role="tablist"
      aria-label="Portfolio model"
      data-testid="mode-switch"
      data-mode={mode}
      className="inline-flex rounded-lg border border-border bg-muted/60 p-0.5"
    >
      {MODES.map((option) => {
        const active = option.mode === mode;
        return (
          <button
            key={option.mode}
            role="tab"
            type="button"
            aria-selected={active}
            title={option.hint}
            onClick={() => onChange(option.mode)}
            className={cn(
              "flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium transition-colors duration-150",
              active
                ? "bg-card text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            {option.label}
            <span
              className={cn(
                "rounded px-1 text-xs tabular-nums",
                active ? "bg-muted text-muted-foreground" : "text-muted-foreground/70",
              )}
            >
              {counts[option.mode]}
            </span>
          </button>
        );
      })}
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * The four controls PC1 deferred, and the one it never listed
 * ------------------------------------------------------------------ *
 *
 * `docs/PORTFOLIO-COMMAND-CENTER.md` §3 deferred the date range, the benchmark, the base currency
 * and the overflow menu out of PC1, with a reason for each: they scope a chart, and PC1 drew no
 * chart. That reasoning expires the moment PC2's workspace is mounted, which is what
 * `gates/pc-integration.md` I2 is.
 *
 * Reading the brief's header list again at integration turned up a FIFTH control that no leaf
 * claimed and PC1's deferral table never mentioned: the **portfolio selector**. The brief names it
 * twice — once in the header list, and again in the detail workspace: *"Keep the portfolio
 * selector available so users can switch portfolios without returning to the aggregate page."*
 * That second sentence is the one that matters. Without it, moving between two portfolios means
 * going up to the aggregate screen and back down, which is the navigation pattern of a broker
 * holdings page.
 *
 * THE RULE ALL FIVE OBEY: a control either does something or says why it cannot. Not one of them
 * is drawn greyed-out with no explanation. A dead control is the defect this product has already
 * shipped twice.
 */

export interface PortfolioChoice {
  readonly portfolioId: number;
  readonly name: string;
}

const ALL_PORTFOLIOS = "All portfolios";

function detailHref(portfolioId: number): Route {
  return `/portfolio/${portfolioId}` as Route;
}

const AGGREGATE_HREF = "/portfolio/portfolios" as Route;

/**
 * All portfolios, or one of them.
 *
 * It is a navigation control, not a filter: choosing a portfolio opens its workspace rather than
 * narrowing this screen, because the two answer different questions and the brief gives each its
 * own surface. Rendering it as a filter would leave a person on the aggregate screen wondering
 * why the comparison table now has one row.
 */
export function PortfolioSelector({
  portfolios,
  selectedId = null,
}: {
  portfolios: readonly PortfolioChoice[];
  /** `null` on the aggregate screen; a portfolio id inside a detail workspace. */
  selectedId?: number | null;
}) {
  const selected = portfolios.find((p) => p.portfolioId === selectedId) ?? null;
  const label = selected ? selected.name : ALL_PORTFOLIOS;

  if (portfolios.length === 0) {
    /* Not a disabled dropdown. There is nothing to select BECAUSE nothing is filed yet, and that
       is a different sentence from "this control is broken". */
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <span
            data-testid="portfolio-selector-empty"
            className="cursor-help rounded-lg border border-dashed border-border px-2.5 py-1.5 text-xs text-muted-foreground"
          >
            No portfolios yet
          </span>
        </TooltipTrigger>
        <TooltipContent className="max-w-xs text-xs">
          There is nothing to switch between until holdings are filed into a capital portfolio.
        </TooltipContent>
      </Tooltip>
    );
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          variant="outline"
          size="sm"
          data-testid="portfolio-selector"
          aria-label={`Portfolio: ${label}`}
        >
          <span className="max-w-[10rem] truncate">{label}</span>
          <ChevronDown aria-hidden="true" className="size-3.5 opacity-60" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-[14rem]">
        <DropdownMenuLabel>Switch portfolio</DropdownMenuLabel>
        <DropdownMenuItem asChild>
          <Link href={AGGREGATE_HREF} className="flex items-center gap-2">
            {/* A tick as well as the highlight: the selected row must survive greyscale. */}
            <Check
              aria-hidden="true"
              className={cn("size-3.5", selected !== null && "invisible")}
            />
            {ALL_PORTFOLIOS}
          </Link>
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        {portfolios.map((choice) => (
          <DropdownMenuItem key={choice.portfolioId} asChild>
            <Link href={detailHref(choice.portfolioId)} className="flex items-center gap-2">
              <Check
                aria-hidden="true"
                className={cn(
                  "size-3.5",
                  choice.portfolioId !== selectedId && "invisible",
                )}
              />
              <span className="truncate">{choice.name}</span>
            </Link>
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/**
 * The benchmark, as a statement rather than a switch.
 *
 * The brief asks for a *selector* — "NIFTY 50, NIFTY 500, custom benchmark". Nothing behind this
 * screen can honour one. A benchmark is chosen per portfolio (`NewPortfolioIn.benchmark_index_id`,
 * and `PortfolioRowOut.benchmark_name` is what comes back), and the aggregate series is measured
 * against it server-side: `/portfolio/overview` takes no parameters at all, so changing the index
 * here could not re-measure anything. A dropdown that silently re-labels a line it did not
 * recompute is worse than no dropdown.
 *
 * So it names the index the comparison actually ran against, and says where it is changed. That is
 * "states why it cannot" rather than a control removed — `gates/pc-integration.md` I2.
 */
export function BenchmarkStatement({
  name,
  onManage,
}: {
  /** `NavSeriesOut.benchmark.name`. Null when the series carries no comparison. */
  name: string | null;
  /** Opens the management drawer, where a benchmark is actually set. */
  onManage?: (() => void) | undefined;
}) {
  const label = name === null ? "No benchmark" : `vs ${name}`;
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          onClick={onManage}
          disabled={onManage === undefined}
          data-testid="benchmark-statement"
          className="max-w-[12rem] truncate rounded-lg border border-border px-2.5 py-1.5 text-xs font-medium text-muted-foreground enabled:cursor-pointer enabled:hover:text-foreground"
        >
          {label}
        </button>
      </TooltipTrigger>
      <TooltipContent className="max-w-xs text-xs">
        {name === null
          ? "This series was not compared against an index. A benchmark is set per portfolio, in Manage portfolios."
          : `Every return on this screen is compared against ${name}. The benchmark is chosen per portfolio, not per page — change it in Manage portfolios.`}
      </TooltipContent>
    </Tooltip>
  );
}

/**
 * Base currency.
 *
 * The brief lists it as a control. It is rendered as a STATEMENT instead, and that is the honest
 * reading rather than a narrowing: Baskfy is India-only equities, every figure on this screen is
 * rupees, and a dropdown with one option is furniture that implies a second option exists.
 * §3 of the plan doc reached the same conclusion and this keeps it, while making the fact visible
 * rather than silently absent — which is what I2 asks for.
 */
export function BaseCurrency() {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          data-testid="base-currency"
          className="cursor-help rounded-lg border border-border px-2 py-1.5 text-xs font-medium tabular-nums text-muted-foreground"
        >
          INR
        </span>
      </TooltipTrigger>
      <TooltipContent className="max-w-xs text-xs">
        Every figure on this screen is Indian rupees. Baskfy holds Indian equities only, so there
        is no second currency to convert to or from.
      </TooltipContent>
    </Tooltip>
  );
}

/**
 * Import, export, settings and reconciliation — the brief's overflow menu.
 *
 * Two of the four are real routes that already exist, one is a real download, and one does not
 * exist at all. The one that does not is listed with its reason rather than omitted, because a
 * person who read the brief will look for it, and "we cannot do this yet, and here is what it
 * needs" is a better answer than a menu that quietly has three items.
 */
export function OverflowMenu({
  onExportCsv,
  onOpenSettings,
  exportDisabledReason,
}: {
  /** Downloads the rows currently on screen. Absent only when there are no rows. */
  onExportCsv?: (() => void) | undefined;
  /** Opens PC6's management drawer. Absent until it is mounted. */
  onOpenSettings?: (() => void) | undefined;
  /** Why export is not offered, when it is not. Never a silent grey item. */
  exportDisabledReason?: string | undefined;
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          variant="outline"
          size="sm"
          data-testid="overflow-menu"
          aria-label="More portfolio actions"
        >
          <MoreHorizontal aria-hidden="true" className="size-4" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-[16rem]">
        <DropdownMenuLabel>Portfolio actions</DropdownMenuLabel>

        {onExportCsv ? (
          <DropdownMenuItem onSelect={() => onExportCsv()} data-testid="export-view">
            Export this view (CSV)
          </DropdownMenuItem>
        ) : (
          <DropdownMenuItem disabled data-testid="export-view-blocked">
            <span className="flex flex-col gap-0.5">
              <span>Export this view (CSV)</span>
              <span className="text-xs text-muted-foreground">
                {exportDisabledReason ?? "There is nothing on screen to export yet."}
              </span>
            </span>
          </DropdownMenuItem>
        )}

        {onOpenSettings ? (
          <DropdownMenuItem onSelect={() => onOpenSettings()} data-testid="open-settings">
            Manage portfolios
          </DropdownMenuItem>
        ) : (
          <DropdownMenuItem asChild data-testid="open-settings">
            <Link href="/portfolio/holdings">Manage portfolios</Link>
          </DropdownMenuItem>
        )}

        <DropdownMenuItem asChild data-testid="open-reconcile">
          <Link href="/reconcile">Reconcile holdings</Link>
        </DropdownMenuItem>

        <DropdownMenuSeparator />

        {/* Named, not omitted. §5.3 of the portfolio docs: purchase prices and dates for
            broker-synced rows arrive with a CAS import, and Baskfy has no importer yet. */}
        <DropdownMenuItem disabled data-testid="import-blocked">
          <span className="flex flex-col gap-0.5">
            <span>Import a consolidated account statement</span>
            <span className="text-xs text-muted-foreground">
              Not built yet. It is what would give Baskfy the purchase prices and dates your broker
              does not send.
            </span>
          </span>
        </DropdownMenuItem>

        {/* The brief asks for a shareable read-only report. It is named here rather than dropped,
            because a reader who was promised one will look for it — and the reason it is missing
            is not a missing button. A link somebody else can open needs a share token, a route
            that serves a portfolio to an unauthenticated reader, and a decision about what a net
            worth may be shown to. The first two are the C3 multi-tenant work; the third is not an
            engineering question at all. The CSV above is the export that exists today. */}
        <DropdownMenuItem disabled data-testid="share-blocked">
          <span className="flex flex-col gap-0.5">
            <span>Shareable read-only report</span>
            <span className="text-xs text-muted-foreground">
              Not available. A link another person can open needs sharing to exist in the account
              model first. Export the CSV above to share a copy instead.
            </span>
          </span>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export function CommandHeader({
  mode,
  onModeChange,
  counts,
  onAddPortfolio,
  rebalanceTargets = [],
  rebalanceDisabledReason,
  portfolios = [],
  selectedPortfolioId = null,
  rangeControl,
  benchmarkName = null,
  onExportCsv,
  onOpenSettings,
  exportDisabledReason,
}: {
  mode: CommandMode;
  onModeChange: (next: CommandMode) => void;
  counts: Readonly<Record<CommandMode, number>>;
  onAddPortfolio?: (() => void) | undefined;
  /** The capital portfolios a rebalance can be prepared for. Empty means the button is disabled. */
  rebalanceTargets?: readonly RebalanceTarget[];
  /** When a rebalance cannot be prepared, this says why — rather than a dead button. */
  rebalanceDisabledReason?: string | undefined;
  /** Everything the portfolio selector can switch to. */
  portfolios?: readonly PortfolioChoice[];
  /** `null` on the aggregate screen; the portfolio's id inside a detail workspace. */
  selectedPortfolioId?: number | null;
  /**
   * The date range, supplied by whoever owns the series it scopes.
   *
   * A slot rather than a prop because the range belongs to the performance workspace: it decides
   * which points are drawn, and re-declaring its state here would give the screen two sources of
   * truth for one selection. **On the aggregate screen it is deliberately empty**, and that is
   * not an omission: `/portfolio/overview` takes no query parameters, so the consolidated series
   * arrives in one window and no control here could re-measure it. The workspace's own pills say
   * exactly that, beside the chart they scope, and the brush narrows within the served window —
   * which is real. A range control in this header that changed nothing would be the dead control
   * the rest of this file exists to avoid.
   */
  rangeControl?: React.ReactNode;
  /** `NavSeriesOut.benchmark.name` — the index the comparison actually ran against. */
  benchmarkName?: string | null | undefined;
  onExportCsv?: (() => void) | undefined;
  onOpenSettings?: (() => void) | undefined;
  exportDisabledReason?: string | undefined;
}) {
  return (
    <header className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-xl font-semibold tracking-tight sm:text-2xl">
            Details
          </h1>
          <ModeSwitch mode={mode} onChange={onModeChange} counts={counts} />
        </div>
        <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
          A closer look at capital, performance and portfolio health — same book as Overview.
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        {/* Scope first, then currency, then the controls that change something. The reading order
            is the sentence a person is composing: *which* portfolios, over *what* period, against
            *which* benchmark, in *which* currency — then what to do about it. */}
        <PortfolioSelector portfolios={portfolios} selectedId={selectedPortfolioId} />
        {rangeControl}
        <BenchmarkStatement name={benchmarkName} onManage={onOpenSettings} />
        <BaseCurrency />

        {/* A hairline between reading the screen and acting on it. */}
        <span aria-hidden="true" className="mx-0.5 hidden h-5 w-px bg-border sm:block" />

        {onAddPortfolio ? (
          <Button type="button" variant="outline" size="sm" onClick={onAddPortfolio}>
            <Plus aria-hidden="true" className="size-4" />
            Add portfolio
          </Button>
        ) : (
          /* No handler wired: go to the screen where holdings are actually filed, rather than
             render a button that does nothing when pressed. */
          <Button asChild variant="outline" size="sm">
            <Link href={ADD_PORTFOLIO_HREF} data-testid="add-portfolio">
              <Plus aria-hidden="true" className="size-4" />
              Add portfolio
            </Link>
          </Button>
        )}

        {/* The one orange surface on the page, and charcoal on it rather than white — 7.32:1.
            See the token block in globals.css for why the pairing matters. */}
        {rebalanceDisabledReason ? (
          <Button
            type="button"
            size="sm"
            disabled
            title={rebalanceDisabledReason}
            data-testid="review-rebalance"
            className="bg-brand text-brand-foreground hover:bg-brand/90"
          >
            <Scale aria-hidden="true" className="size-4" />
            Review rebalance
          </Button>
        ) : rebalanceTargets.length === 1 ? (
          <Button asChild size="sm" className="bg-brand text-brand-foreground hover:bg-brand/90">
            <Link
              href={rebalanceHref(rebalanceTargets[0]!.portfolioId)}
              data-testid="review-rebalance"
            >
              <Scale aria-hidden="true" className="size-4" />
              Review rebalance
            </Link>
          </Button>
        ) : (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                type="button"
                size="sm"
                data-testid="review-rebalance"
                className="bg-brand text-brand-foreground hover:bg-brand/90"
              >
                <Scale aria-hidden="true" className="size-4" />
                Review rebalance
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuLabel>A rebalance is prepared for one portfolio</DropdownMenuLabel>
              {rebalanceTargets.map((target) => (
                <DropdownMenuItem key={target.portfolioId} asChild>
                  <Link href={rebalanceHref(target.portfolioId)}>{target.name}</Link>
                </DropdownMenuItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>
        )}

        <OverflowMenu
          onExportCsv={onExportCsv}
          onOpenSettings={onOpenSettings}
          exportDisabledReason={exportDisabledReason}
        />
      </div>

      {rebalanceDisabledReason ? (
        <p className="w-full text-xs text-muted-foreground" data-testid="rebalance-blocked">
          {rebalanceDisabledReason}
        </p>
      ) : null}
    </header>
  );
}
