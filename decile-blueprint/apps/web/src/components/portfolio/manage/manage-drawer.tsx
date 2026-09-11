"use client";

import * as DialogPrimitive from "@radix-ui/react-dialog";
import { useState } from "react";
import { ArrowLeft, CheckCircle2, ChevronRight, X } from "lucide-react";

import { ConnectionsPanel } from "@/components/portfolio/manage/connections-panel";
import { DeletePanel } from "@/components/portfolio/manage/delete-panel";
import { HoldingsTransfer } from "@/components/portfolio/manage/holdings-transfer";
import { Unavailable } from "@/components/portfolio/manage/panel-chrome";
import { CreatePortfolioPanel, RenamePanel } from "@/components/portfolio/manage/portfolio-form";
import { SleeveForm } from "@/components/portfolio/manage/sleeve-form";
import { ViewBuilder } from "@/components/portfolio/manage/view-builder";
import { Button } from "@/components/ui/button";
import { Dialog, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import {
  GROUP_LABEL,
  actionById,
  unavailableActions,
  type ManageActionId,
  type ManageGroup,
  type ManageOutcome,
  type SleeveDraft,
  type SleeveRow,
  type TransferRequest,
} from "@/lib/portfolio/manage";
import type { AggregatedHolding, BrokerRef, PortfolioDraft } from "@/lib/portfolio/organize";
import type { PortfolioRow } from "@/lib/portfolio/overview";
import { cn } from "@/lib/utils";

/**
 * **Manage portfolios** — every configuration decision in one drawer, off the dashboard.
 *
 * WHY A DRAWER AND WHY EVERYTHING IN IT
 * -------------------------------------
 * The command centre answers *"how am I doing"*. Renaming a portfolio, moving a holding and
 * deleting a grouping answer *"how is this set up"*, and mixing the two puts a destructive button
 * on an analytical surface. Worse, before this leaf they were not in one place at all: creating
 * was an inline flow under the table, organising was a `<details>` further down, sleeves were a
 * separate route and deleting had no screen at all. Four places to change a portfolio is four
 * places for the model's rules to be stated differently.
 *
 * So the drawer is the single home, and the rules are stated once inside it: exclusivity in the
 * transfer preview, the monitoring-view notice wherever a lens is made, the fate of the holdings
 * on the delete confirmation.
 *
 * WHAT IS NAMED AND NOT DRAWN
 * ---------------------------
 * Archive, permissions, ownership, audit history — and objective, which §6.3 did not know about.
 * Each is listed at the foot of the index with what it would do, why it cannot, and what would
 * unblock it. None of them has a button. A greyed-out control is still a control: it occupies the
 * place a working one would, invites the click that teaches a person the product is broken, and
 * says nothing about when it might work.
 *
 * KEYBOARD (G8), AND THE TWO THINGS RADIX DOES NOT DO FOR US
 * -----------------------------------------------------------
 * Radix supplies `role="dialog"`, the accessible name from {@link DialogTitle}, the focus trap
 * and Escape. Two things it does not, and both were found by writing the test rather than by
 * reading the docs:
 *
 *   1. **`aria-modal` is never set.** Radix 1.1.23 relies on `RemoveScroll` and on hiding the
 *      siblings, which is correct behaviour and is not the same as *telling* assistive tech that
 *      the rest of the page is inert. It is set here explicitly.
 *   2. **Focus is not returned to the trigger**, because this drawer is controlled from outside
 *      and has no `DialogTrigger` for Radix to return to — the host owns the button. So the
 *      element that was focused at the moment the drawer opened is captured during the render
 *      that opens it (before the commit that moves focus inside) and restored in
 *      `onCloseAutoFocus`. A dialog that closes onto `document.body` leaves a keyboard user at
 *      the top of the page with no idea where they were.
 */

/** The actions with a panel of their own. Benchmark and objective live inside the forms where
 *  they would be looked for; reconciliation lives with the brokers it comes from. */
const NAV: readonly ManageActionId[] = [
  "create",
  "rename",
  "assign",
  "move",
  "watch",
  "sleeve",
  "broker",
  "delete",
];

const GROUP_ORDER: readonly ManageGroup[] = [
  "identity",
  "holdings",
  "structure",
  "connections",
  "danger",
];

export interface ManageHandlers {
  /** `POST /api/v1/portfolio`. Shared with the existing new-portfolio flow's `onCreate`. */
  create?: ((draft: PortfolioDraft) => Promise<ManageOutcome>) | undefined;
  /** `PATCH /api/v1/portfolios/{id}` with a name. */
  rename?: ((portfolioId: number, name: string) => Promise<ManageOutcome>) | undefined;
  /** `POST /api/v1/portfolio/{id}/holdings`. Serves assign, move and watch alike. */
  transfer?: ((request: TransferRequest) => Promise<ManageOutcome>) | undefined;
  /** `GET /api/v1/portfolios/{id}/sleeves`. Required before any sleeve save — see SleeveForm. */
  loadSleeves?: ((portfolioId: number) => Promise<readonly SleeveRow[]>) | undefined;
  /** `PUT /api/v1/portfolios/{id}/sleeves`. Replaces the whole division. */
  saveSleeves?:
    | ((portfolioId: number, sleeves: readonly SleeveDraft[]) => Promise<ManageOutcome>)
    | undefined;
  /** `PATCH /api/v1/portfolios/{id}` with a broker account. */
  reattribute?:
    | ((portfolioId: number, brokerAccountId: number | null) => Promise<ManageOutcome>)
    | undefined;
  /** `DELETE /api/v1/portfolios/{id}`. */
  remove?: ((portfolioId: number) => Promise<ManageOutcome>) | undefined;
}

export interface ManagePortfoliosDrawerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Capital portfolios — `OverviewOut.portfolios`. */
  capital: readonly PortfolioRow[];
  /** Monitoring views — `OverviewOut.monitoring_views`. */
  views: readonly PortfolioRow[];
  /** Every aggregated holding — `HoldingsOut.holdings`. Empty is a valid first-run state. */
  rows: readonly AggregatedHolding[];
  brokers?: readonly BrokerRef[] | undefined;
  sectors?: Readonly<Record<string, string>> | undefined;
  benchmarks?: readonly string[] | undefined;
  /** `OverviewOut.open_reconciliation_count`. */
  openReconciliationCount?: number | undefined;
  handlers?: ManageHandlers | undefined;
  /** Which panel to open on. Null opens the index. */
  initialAction?: ManageActionId | null | undefined;
  /** Fired after any write that genuinely succeeded, so the host can refetch. */
  onChanged?: ((message: string) => void) | undefined;
}

export function ManagePortfoliosDrawer({
  open,
  onOpenChange,
  capital,
  views,
  rows,
  brokers = [],
  sectors,
  benchmarks,
  openReconciliationCount = 0,
  handlers = {},
  initialAction = null,
  onChanged,
}: ManagePortfoliosDrawerProps) {
  const [panel, setPanel] = useState<ManageActionId | null>(initialAction);
  const [confirmation, setConfirmation] = useState<string | null>(null);

  /* Captured during the render that opens the drawer, which is the last moment at which the
     trigger still has focus — a `useEffect` would run after Radix's own effect has already moved
     focus into the content, and would capture the content instead. Held in state rather than a
     ref because a ref read during render is exactly the stale-UI hazard the lint rule is there
     to catch; the node is a value this component renders against, so state is the honest home.
     Same render-phase-state trick the shipped inspector drawer uses to reset on a different row. */
  const [wasOpen, setWasOpen] = useState(open);
  const [restoreFocusTo, setRestoreFocusTo] = useState<HTMLElement | null>(null);
  if (open !== wasOpen) {
    setWasOpen(open);
    if (open) {
      const active = document.activeElement;
      setRestoreFocusTo(active instanceof HTMLElement ? active : null);
    }
  }

  const everything = [...capital, ...views];

  /* ---------------------------------------------------------------- *
   * Undo, and the one change it is honest to offer it for
   * ---------------------------------------------------------------- *
   *
   * Brief: *"Undo for organisational changes."* Of the seven writes here, exactly one is both
   * consequential and EXACTLY reversible: a move. Its reverse is the same request with the two
   * portfolios swapped, and the API will honour it.
   *
   * The others are deliberately not offered one, because an undo that cannot undo is worse than
   * none. A rename is reversed by renaming — the form is right there and nothing was lost. A
   * **delete is not reversible at all**: `portfolio_nav_daily` and `portfolio_cash_flow` cascade,
   * so the stored NAV series, the return since `started_on` and the dated flows XIRR is solved
   * from are gone. `DeletePanel` says exactly that before it asks for the name to be typed, and
   * an Undo button beside a delete would contradict it.
   *
   * An undo is a fresh MOVE and is recorded as one. There is no rollback anywhere in this
   * product; what there is, is a second move that puts the shares back where they were.
   */
  const [undoable, setUndoable] = useState<TransferRequest | null>(null);
  const [undoing, setUndoing] = useState(false);

  const transfer = handlers.transfer;
  const wired: ManageHandlers = {
    ...handlers,
    ...(transfer
      ? {
          transfer: async (request: TransferRequest) => {
            const outcome = await transfer(request);
            /* Only a MOVE with a known source, and only on success. A failed write moved
               nothing, so there is nothing to put back — offering undo after one would invite a
               person to "undo" a change that never happened. */
            setUndoable(
              outcome.ok && request.intent === "MOVE" && request.sourcePortfolioId !== null
                ? request
                : null,
            );
            return outcome;
          },
        }
      : {}),
  };

  const undoTarget =
    undoable === null
      ? null
      : (everything.find((row) => row.portfolio_id === undoable.sourcePortfolioId) ?? null);

  async function undo(): Promise<void> {
    if (undoable === null || undoable.sourcePortfolioId === null || transfer === undefined) return;
    setUndoing(true);
    const back: TransferRequest = {
      intent: "MOVE",
      destinationPortfolioId: undoable.sourcePortfolioId,
      sourcePortfolioId: undoable.destinationPortfolioId,
      holdings: undoable.holdings,
    };
    const outcome = await transfer(back);
    setUndoing(false);
    setUndoable(null);
    if (outcome.ok) {
      saved(`Moved back into ${undoTarget?.name ?? "the portfolio it came from"}.`);
    } else {
      /* A failed undo says so in the server's own words and leaves the shares where the move put
         them. Silence here would read as "put back", which is the one thing it is not. */
      setConfirmation(null);
      setUndoError(outcome.reason);
    }
  }

  const [undoError, setUndoError] = useState<string | null>(null);

  function go(next: ManageActionId | null): void {
    setPanel(next);
    setConfirmation(null);
    setUndoable(null);
    setUndoError(null);
  }

  function saved(message: string): void {
    setConfirmation(message);
    setUndoError(null);
    onChanged?.(message);
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) {
          setPanel(initialAction);
          setConfirmation(null);
        }
        onOpenChange(next);
      }}
    >
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay
          className={cn(
            "fixed inset-0 z-50 bg-foreground/25",
            "data-[state=open]:animate-in data-[state=open]:fade-in-0",
            "data-[state=closed]:animate-out data-[state=closed]:fade-out-0",
            "motion-safe:duration-200 motion-reduce:animate-none",
          )}
        />
        <DialogPrimitive.Content
          data-testid="manage-drawer"
          data-panel={panel ?? "index"}
          aria-modal="true"
          onCloseAutoFocus={(event) => {
            if (restoreFocusTo === null || !restoreFocusTo.isConnected) return;
            event.preventDefault();
            restoreFocusTo.focus();
          }}
          className={cn(
            "fixed z-50 flex flex-col gap-4 overflow-y-auto overscroll-contain border-border bg-card p-5 shadow-lg outline-none",
            "inset-x-0 bottom-0 max-h-[90vh] rounded-t-[26px] border-t",
            "min-[900px]:inset-y-0 min-[900px]:right-0 min-[900px]:left-auto min-[900px]:max-h-none",
            "min-[900px]:w-[52vw] min-[900px]:min-w-[520px] min-[900px]:max-w-[820px]",
            "min-[900px]:rounded-l-[26px] min-[900px]:rounded-t-none min-[900px]:border-l min-[900px]:border-t-0",
            "data-[state=open]:animate-in data-[state=closed]:animate-out",
            "data-[state=open]:fade-in-0 data-[state=closed]:fade-out-0",
            "min-[900px]:data-[state=open]:slide-in-from-right-4",
            "motion-safe:duration-200 motion-reduce:animate-none",
          )}
        >
          <div
            aria-hidden="true"
            className="mx-auto h-1 w-10 shrink-0 rounded-full bg-muted-foreground/30 min-[900px]:hidden"
          />

          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0 space-y-1">
              <DialogTitle className="text-lg font-semibold">Manage portfolios</DialogTitle>
              <DialogDescription className="text-xs leading-relaxed text-muted-foreground">
                Everything that changes how your portfolios are set up. Nothing here places an
                order — the shares are already yours, and this only records which portfolio each
                one belongs to.
              </DialogDescription>
            </div>
            <DialogPrimitive.Close asChild>
              <Button type="button" variant="ghost" size="icon" aria-label="Close manage portfolios">
                <X aria-hidden="true" className="size-4" />
              </Button>
            </DialogPrimitive.Close>
          </div>

          {/* Announced as well as drawn: a person using a screen reader gets no signal from a
              panel changing under them. */}
          <div role="status" aria-live="polite" className="empty:hidden">
            {confirmation === null ? null : (
              <p
                data-testid="manage-confirmation"
                className="flex items-start gap-2 rounded-lg border border-positive/40 bg-positive-muted px-3 py-2 text-xs leading-relaxed text-foreground"
              >
                <CheckCircle2 aria-hidden="true" className="mt-px size-4 shrink-0 text-positive" />
                <span className="flex-1">
                  <strong className="font-semibold">Saved.</strong> {confirmation}
                  {undoable !== null && undoTarget !== null ? (
                    <>
                      {" "}
                      <button
                        type="button"
                        onClick={() => void undo()}
                        disabled={undoing}
                        data-testid="manage-undo"
                        className="font-medium underline underline-offset-2 disabled:opacity-60"
                      >
                        {undoing ? "Moving back…" : `Undo — move back into ${undoTarget.name}`}
                      </button>
                    </>
                  ) : null}
                </span>
              </p>
            )}
            {undoError === null ? null : (
              <p
                data-testid="manage-undo-failed"
                className="flex items-start gap-2 rounded-lg border border-negative/40 bg-negative-muted px-3 py-2 text-xs leading-relaxed text-foreground"
              >
                <span aria-hidden="true" className="font-semibold">
                  !
                </span>
                <span>
                  <strong className="font-semibold">Not put back.</strong> {undoError} The shares
                  are where the move left them.
                </span>
              </p>
            )}
          </div>

          {panel === null ? (
            <ManageIndex onOpen={go} />
          ) : (
            <div className="space-y-4">
              <Button type="button" variant="ghost" size="sm" onClick={() => go(null)}>
                <ArrowLeft aria-hidden="true" />
                All actions
              </Button>

              {panel === "create" ? (
                <CreatePortfolioPanel
                  rows={rows}
                  sectors={sectors}
                  benchmarks={benchmarks}
                  onCreate={wired.create}
                  onSaved={saved}
                  onSwitchToViews={() => go("watch")}
                />
              ) : null}

              {panel === "rename" ? (
                <RenamePanel
                  portfolios={everything}
                  onRename={wired.rename}
                  onSaved={saved}
                />
              ) : null}

              {panel === "assign" || panel === "move" ? (
                <HoldingsTransfer
                  intent={panel === "move" ? "MOVE" : "ASSIGN"}
                  rows={rows}
                  sectors={sectors}
                  capital={capital}
                  onTransfer={wired.transfer}
                  onSaved={saved}
                />
              ) : null}

              {panel === "watch" ? (
                <ViewBuilder
                  rows={rows}
                  views={views}
                  onCreate={wired.create}
                  onTransfer={wired.transfer}
                  onSaved={saved}
                />
              ) : null}

              {panel === "sleeve" ? (
                <SleeveForm
                  portfolios={capital}
                  loadSleeves={wired.loadSleeves}
                  onSaveSleeves={wired.saveSleeves}
                  onSaved={saved}
                />
              ) : null}

              {panel === "broker" ? (
                <ConnectionsPanel
                  portfolios={everything}
                  brokers={brokers}
                  openReconciliationCount={openReconciliationCount}
                  onReattribute={wired.reattribute}
                  onSaved={saved}
                />
              ) : null}

              {panel === "delete" ? (
                <DeletePanel
                  portfolios={everything}
                  onDelete={wired.remove}
                  onSaved={saved}
                />
              ) : null}
            </div>
          )}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </Dialog>
  );
}

/** The index: every action that has a panel, then everything that has not got an endpoint. */
function ManageIndex({ onOpen }: { onOpen: (id: ManageActionId) => void }) {
  const missing = unavailableActions();
  return (
    <div className="space-y-5" data-testid="manage-index">
      {GROUP_ORDER.map((group) => {
        const entries = NAV.map(actionById).filter((action) => action.group === group);
        if (entries.length === 0) return null;
        return (
          <section key={group} className="space-y-2">
            <h3 className="text-[0.6875rem] font-medium uppercase tracking-wide text-muted-foreground">
              {GROUP_LABEL[group]}
            </h3>
            <ul className="divide-y divide-border/70 rounded-xl border border-border bg-background">
              {entries.map((action) => (
                <li key={action.id}>
                  <button
                    type="button"
                    data-testid={`manage-action-${action.id}`}
                    data-endpoint={
                      action.availability.kind === "unavailable"
                        ? undefined
                        : `${action.availability.endpoint.method} ${action.availability.endpoint.path}`
                    }
                    onClick={() => onOpen(action.id)}
                    className="flex w-full items-start gap-3 px-3 py-2.5 text-left transition-colors duration-100 hover:bg-muted"
                  >
                    <span className="min-w-0 flex-1">
                      <span className="block text-sm font-medium">{action.title}</span>
                      <span className="block text-xs leading-snug text-muted-foreground">
                        {action.blurb}
                      </span>
                    </span>
                    <ChevronRight
                      aria-hidden="true"
                      className="mt-0.5 size-4 shrink-0 text-muted-foreground"
                    />
                  </button>
                </li>
              ))}
            </ul>
          </section>
        );
      })}

      <section className="space-y-2" data-testid="manage-unavailable">
        <h3 className="text-[0.6875rem] font-medium uppercase tracking-wide text-muted-foreground">
          Not available yet
        </h3>
        <p className="text-xs leading-relaxed text-muted-foreground">
          These are named rather than drawn. Baskfy has no endpoint behind any of them, so a
          control here would be a button that does nothing — each says why, and what would have to
          exist first.
        </p>
        <ul className="divide-y divide-border/70 rounded-xl border border-dashed border-border bg-muted/30 px-3">
          {missing.map((action) => (
            <Unavailable key={action.id} action={action} />
          ))}
        </ul>
      </section>
    </div>
  );
}
