"use client";

import type { Route } from "next";
import Link from "next/link";
import { Plus, Scale } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
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

export function CommandHeader({
  mode,
  onModeChange,
  counts,
  onAddPortfolio,
  rebalanceTargets = [],
  rebalanceDisabledReason,
}: {
  mode: CommandMode;
  onModeChange: (next: CommandMode) => void;
  counts: Readonly<Record<CommandMode, number>>;
  onAddPortfolio?: (() => void) | undefined;
  /** The capital portfolios a rebalance can be prepared for. Empty means the button is disabled. */
  rebalanceTargets?: readonly RebalanceTarget[];
  /** When a rebalance cannot be prepared, this says why — rather than a dead button. */
  rebalanceDisabledReason?: string | undefined;
}) {
  return (
    <header className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-xl font-semibold tracking-tight sm:text-2xl">
            Portfolio Command Center
          </h1>
          <ModeSwitch mode={mode} onChange={onModeChange} counts={counts} />
        </div>
        <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
          One reconciled view of your capital, performance and portfolio health.
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-2">
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
      </div>

      {rebalanceDisabledReason ? (
        <p className="w-full text-xs text-muted-foreground" data-testid="rebalance-blocked">
          {rebalanceDisabledReason}
        </p>
      ) : null}
    </header>
  );
}
