"use client";

import type { RebalanceOut } from "@baskfy/api-client";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { ArrowLeft, ArrowRight, CalendarClock, CircleAlert, Play, Scale, X } from "lucide-react";
import { useState, type ReactNode } from "react";

import { ConfirmStep } from "@/components/portfolio/rebalance/confirm-step";
import { ImpactPanel } from "@/components/portfolio/rebalance/impact-panel";
import { PlanSheet } from "@/components/portfolio/rebalance/plan-sheet";
import { ListSummary, WeightComparison } from "@/components/portfolio/rebalance/weight-comparison";
import { Button } from "@/components/ui/button";
import { Dialog, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import type { PortfolioDetail } from "@/lib/portfolio/overview";
import {
  WORKFLOW,
  rebalancePreview,
  stepBlockedReason,
  type ScreenRef,
  type StepKey,
} from "@/lib/portfolio/rebalance-preview";
import { cn } from "@/lib/utils";

/**
 * "Review rebalance" — a large drawer over the page, showing what *would* change.
 *
 * ## The one rule that shapes everything in here
 *
 * **It never places an order, and there is no code path from it to one.** Baskfy's web app has no
 * route to a broker; the desk's gateway is a separate service behind the desk's seven
 * non-negotiables, and the weekly rebalancer has never had an automatic path. The fifth step of
 * this workflow produces a document. `gates/pc4.md` G4 greps this whole directory for an order
 * route and expects nothing, which is a cheaper guard than remembering.
 *
 * ## What it shows, and what it says it cannot
 *
 * `RebalanceOut` carries four lists of names and a set of equal target weights. That makes the
 * weight comparison — current against target, name by name — completely real, and it is given the
 * room. Everything else the brief asks for (quantities, cash, turnover, brokerage, tax lots,
 * liquidity, beta) needs a price, a cost model, a purchase date or a statistics job that does not
 * exist, and is listed by name on the Impact step with where each figure genuinely comes from.
 * Nothing is estimated, and nothing renders as a bare dash.
 *
 * ## Wiring
 *
 * The drawer owns no fetching. A parent passes the proposal, the portfolio detail and the screen
 * list, and gets `onAnalyse` back when the reader asks for a fresh diff — the same division PC1
 * uses, and the reason five leaves could be built against one page at once. The exact contract is
 * in `docs/pc-findings/pc4.md`.
 */

const SHELL =
  "fixed z-50 flex flex-col border-border bg-card shadow-lg outline-none " +
  "inset-x-0 bottom-0 max-h-[92vh] rounded-t-[26px] border-t " +
  "min-[900px]:inset-y-0 min-[900px]:right-0 min-[900px]:left-auto min-[900px]:max-h-none " +
  "min-[900px]:w-[68vw] min-[900px]:min-w-[520px] min-[900px]:max-w-[1100px] " +
  "min-[900px]:rounded-l-[26px] min-[900px]:rounded-t-none min-[900px]:border-l min-[900px]:border-t-0 " +
  "data-[state=open]:animate-in data-[state=closed]:animate-out " +
  "data-[state=open]:fade-in-0 data-[state=closed]:fade-out-0 " +
  "min-[900px]:data-[state=open]:slide-in-from-right-4 " +
  "motion-safe:duration-200 motion-reduce:animate-none";

export interface AnalyseInput {
  readonly screenPublicId: string;
  readonly topN: number;
  readonly holdBuffer: number;
}

export interface RebalanceDrawerProps {
  portfolioName: string;
  /** `GET /portfolio/{id}` — holdings with weights. Null degrades honestly; it does not crash. */
  detail: PortfolioDetail | null;
  /** Every screen the account owns. An empty list is the blocked state, not an empty drawer. */
  screens: readonly ScreenRef[];
  /** The computed proposal, or null before the first run. */
  rebalance: RebalanceOut | null;
  /** Asked when the reader wants a fresh diff. Omit it and the Analyse controls are read-only. */
  onAnalyse?: ((input: AnalyseInput) => void) | undefined;
  analysing?: boolean | undefined;
  /** The server's own words when a diff failed. */
  analyseError?: string | null | undefined;
  defaultTopN?: number | undefined;
  defaultHoldBuffer?: number | undefined;
  /** The control that opens the drawer. Focus returns to it on close. */
  trigger?: ReactNode | undefined;
  open?: boolean | undefined;
  onOpenChange?: ((open: boolean) => void) | undefined;
}

const DEFAULT_TOP_N = 20;
const DEFAULT_HOLD_BUFFER = 10;

/** The typed text as a number the API will accept; an empty or nonsense field means the default. */
function parsePositive(raw: string, min: number, fallback: number): number {
  const parsed = Number.parseInt(raw, 10);
  if (Number.isNaN(parsed)) return fallback;
  return Math.max(min, parsed);
}

export function RebalanceDrawer({
  portfolioName,
  detail,
  screens,
  rebalance,
  onAnalyse,
  analysing = false,
  analyseError = null,
  defaultTopN = DEFAULT_TOP_N,
  defaultHoldBuffer = DEFAULT_HOLD_BUFFER,
  trigger,
  open,
  onOpenChange,
}: RebalanceDrawerProps) {
  const [internalOpen, setInternalOpen] = useState(false);
  const [step, setStep] = useState<StepKey>("analyse");
  const [excluded, setExcluded] = useState<ReadonlySet<number>>(new Set<number>());
  const [notes, setNotes] = useState<ReadonlyMap<number, string>>(new Map<number, string>());
  const [acknowledged, setAcknowledged] = useState(false);
  const [screenId, setScreenId] = useState("");
  const [topNText, setTopNText] = useState(String(defaultTopN));
  const [holdBufferText, setHoldBufferText] = useState(String(defaultHoldBuffer));

  /* A fresh diff is a fresh set of names, so exclusions and notes pinned to the old instrument ids
     are answers to a question nobody asked. Adjusted during render rather than in an effect — the
     React docs' "adjusting state when a prop changes", the same shape `inspector-drawer` uses —
     because clearing them in an effect would render one frame of the new proposal wearing the old
     reader's exclusions. */
  const [trackedId, setTrackedId] = useState<number | null>(rebalance?.id ?? null);
  if ((rebalance?.id ?? null) !== trackedId) {
    setTrackedId(rebalance?.id ?? null);
    setExcluded(new Set<number>());
    setNotes(new Map<number, string>());
    setAcknowledged(false);
  }

  const isOpen = open ?? internalOpen;
  function setOpen(next: boolean) {
    setInternalOpen(next);
    onOpenChange?.(next);
  }

  const preview = rebalancePreview({ portfolioName, rebalance, detail, screens, excluded });
  const chosenScreen = screenId || screens[0]?.public_id || "";
  const topN = parsePositive(topNText, 1, defaultTopN);
  const holdBuffer = parsePositive(holdBufferText, 0, defaultHoldBuffer);

  function toggleExclude(instrumentId: number) {
    setExcluded((current) => {
      const next = new Set(current);
      if (next.has(instrumentId)) next.delete(instrumentId);
      else next.add(instrumentId);
      return next;
    });
    /* An exclusion changes the impact and the plan, so an acknowledgement given before it is no
       longer an acknowledgement of what the plan now says. */
    setAcknowledged(false);
  }

  function setNote(instrumentId: number, note: string) {
    setNotes((current) => {
      const next = new Map(current);
      if (note === "") next.delete(instrumentId);
      else next.set(instrumentId, note);
      return next;
    });
  }

  const index = WORKFLOW.findIndex((entry) => entry.key === step);
  const current = WORKFLOW[index] ?? WORKFLOW[0];
  const previousStep = index > 0 ? WORKFLOW[index - 1] : undefined;
  const nextStep = index < WORKFLOW.length - 1 ? WORKFLOW[index + 1] : undefined;
  const nextBlocked = nextStep ? stepBlockedReason(nextStep.key, preview, acknowledged) : null;

  return (
    <Dialog open={isOpen} onOpenChange={setOpen}>
      <DialogPrimitive.Trigger asChild>
        {trigger ?? (
          <Button variant="primary" size="sm" data-testid="open-rebalance-drawer">
            <Scale aria-hidden="true" />
            Review rebalance
          </Button>
        )}
      </DialogPrimitive.Trigger>

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
          data-testid="rebalance-drawer"
          /* Radix makes the dialog modal by marking the rest of the tree `aria-hidden`, which is
             the stronger guarantee, but it does not set `aria-modal`. Both are stated: a reader
             on a tool that leans on the attribute is told this is modal, and one on a tool that
             walks the tree finds nothing outside it. */
          aria-modal="true"
          className={SHELL}
        >
          <div
            aria-hidden="true"
            className="mx-auto mt-2 h-1 w-10 shrink-0 rounded-full bg-muted-foreground/30 min-[900px]:hidden"
          />

          <header className="flex shrink-0 items-start justify-between gap-3 border-b border-border px-4 py-3 min-[900px]:px-5">
            <div className="min-w-0">
              <DialogTitle className="truncate text-lg font-semibold tracking-tight">
                Review rebalance · {portfolioName}
              </DialogTitle>
              <DialogDescription className="mt-0.5 text-xs leading-snug text-muted-foreground">
                What would change if you acted on this screen. Nothing here is sent to a broker —
                the last step is a plan you take to yours.
              </DialogDescription>
              {preview.status === "ready" ? (
                <p className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-muted-foreground">
                  <span className="flex items-center gap-1">
                    <CalendarClock aria-hidden="true" className="size-3.5 shrink-0" />
                    Screen data as of {preview.asOf}
                  </span>
                  <span>
                    {preview.screenName} · top {preview.topN} · hold buffer {preview.holdBuffer}
                  </span>
                </p>
              ) : null}
            </div>
            <DialogPrimitive.Close asChild>
              <Button variant="ghost" size="icon" className="size-8 shrink-0" aria-label="Close rebalance review">
                <X aria-hidden="true" className="size-4" />
              </Button>
            </DialogPrimitive.Close>
          </header>

          {preview.status === "no-screen" && preview.blocker !== null ? (
            <div className="flex flex-1 items-start gap-3 px-4 py-6 min-[900px]:px-5" data-testid="no-screen-state">
              <CircleAlert aria-hidden="true" className="mt-0.5 size-5 shrink-0 text-warning" />
              <div className="max-w-xl">
                <h3 className="text-base font-semibold">{preview.blocker.headline}</h3>
                <p className="mt-1 text-sm leading-relaxed text-muted-foreground">
                  {preview.blocker.detail}
                </p>
                <Button variant="primary" size="sm" className="mt-3" asChild>
                  <a href={preview.blocker.action.href} data-testid="no-screen-action">
                    {preview.blocker.action.label}
                    <ArrowRight aria-hidden="true" />
                  </a>
                </Button>
              </div>
            </div>
          ) : (
            <>
              <nav aria-label="Rebalance workflow" className="shrink-0 border-b border-border px-4 py-2 min-[900px]:px-5">
                <ol className="flex flex-wrap gap-1" data-testid="workflow-steps">
                  {WORKFLOW.map((entry, position) => {
                    const blocked = stepBlockedReason(entry.key, preview, acknowledged);
                    const active = entry.key === step;
                    return (
                      <li key={entry.key}>
                        <button
                          type="button"
                          disabled={blocked !== null}
                          aria-current={active ? "step" : undefined}
                          title={blocked ?? entry.purpose}
                          data-testid={`step-${entry.key}`}
                          onClick={() => setStep(entry.key)}
                          className={cn(
                            "flex items-center gap-1.5 rounded-md px-2 py-1 text-xs font-medium transition-colors duration-150",
                            active
                              ? "bg-brand-muted text-brand-strong"
                              : "text-muted-foreground hover:bg-muted hover:text-foreground",
                            blocked !== null && "cursor-not-allowed opacity-50 hover:bg-transparent",
                          )}
                        >
                          <span className="tabular-nums opacity-70">{position + 1}</span>
                          {entry.title}
                        </button>
                      </li>
                    );
                  })}
                </ol>
              </nav>

              <div className="min-h-0 flex-1 space-y-4 overflow-y-auto overscroll-contain px-4 py-4 min-[900px]:px-5">
                <div>
                  <h2 className="text-sm font-semibold">
                    {index + 1}. {current?.title}
                  </h2>
                  <p className="mt-0.5 text-xs leading-snug text-muted-foreground">
                    {current?.purpose}
                  </p>
                </div>

                {step === "analyse" ? (
                  <AnalyseStep
                    screens={screens}
                    chosenScreen={chosenScreen}
                    onScreen={setScreenId}
                    topNText={topNText}
                    onTopN={setTopNText}
                    holdBufferText={holdBufferText}
                    onHoldBuffer={setHoldBufferText}
                    analysing={analysing}
                    analyseError={analyseError}
                    canRun={onAnalyse !== undefined && chosenScreen !== ""}
                    onRun={() => onAnalyse?.({ screenPublicId: chosenScreen, topN, holdBuffer })}
                    preview={preview}
                  />
                ) : null}

                {step === "adjust" ? (
                  <AdjustStep
                    preview={preview}
                    notes={notes}
                    onToggleExclude={toggleExclude}
                    onNote={setNote}
                    onRestoreAll={() => {
                      setExcluded(new Set<number>());
                      setAcknowledged(false);
                    }}
                  />
                ) : null}

                {step === "impact" ? <ImpactPanel preview={preview} /> : null}

                {step === "confirm" ? (
                  <ConfirmStep
                    preview={preview}
                    acknowledged={acknowledged}
                    onAcknowledge={setAcknowledged}
                  />
                ) : null}

                {step === "plan" ? <PlanSheet preview={preview} notes={notes} /> : null}
              </div>

              <footer className="flex shrink-0 flex-wrap items-center gap-3 border-t border-border px-4 py-3 min-[900px]:px-5">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={previousStep === undefined}
                  data-testid="step-back"
                  onClick={() => previousStep && setStep(previousStep.key)}
                >
                  <ArrowLeft aria-hidden="true" />
                  {previousStep ? previousStep.title : "Back"}
                </Button>

                {nextStep ? (
                  <>
                    <Button
                      variant="primary"
                      size="sm"
                      disabled={nextBlocked !== null}
                      data-testid="step-next"
                      onClick={() => setStep(nextStep.key)}
                    >
                      {nextStep.title}
                      <ArrowRight aria-hidden="true" />
                    </Button>
                    {nextBlocked !== null ? (
                      <p
                        data-testid="step-next-blocked"
                        className="flex-1 text-xs leading-snug text-muted-foreground"
                      >
                        {nextBlocked}
                      </p>
                    ) : null}
                  </>
                ) : (
                  <p className="flex-1 text-xs leading-snug text-muted-foreground">
                    That is the whole review. The plan above is a document — Baskfy does not send
                    it, and no control here does.
                  </p>
                )}
              </footer>
            </>
          )}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </Dialog>
  );
}

function AnalyseStep({
  screens,
  chosenScreen,
  onScreen,
  topNText,
  onTopN,
  holdBufferText,
  onHoldBuffer,
  analysing,
  analyseError,
  canRun,
  onRun,
  preview,
}: {
  screens: readonly ScreenRef[];
  chosenScreen: string;
  onScreen: (value: string) => void;
  topNText: string;
  onTopN: (value: string) => void;
  holdBufferText: string;
  onHoldBuffer: (value: string) => void;
  analysing: boolean;
  analyseError: string | null;
  canRun: boolean;
  onRun: () => void;
  preview: ReturnType<typeof rebalancePreview>;
}) {
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3 rounded-xl border border-border bg-card px-3 py-3">
        <div className="min-w-56 flex-1 space-y-1">
          <Label htmlFor="rebalance-drawer-screen">Screen</Label>
          <Select
            id="rebalance-drawer-screen"
            value={chosenScreen}
            data-testid="drawer-screen-select"
            onChange={(event) => onScreen(event.target.value)}
          >
            {screens.map((screen) => (
              <option key={screen.public_id} value={screen.public_id}>
                {screen.name}
              </option>
            ))}
          </Select>
        </div>
        <div className="w-24 space-y-1">
          <Label htmlFor="rebalance-drawer-top-n">Top N</Label>
          <Input
            id="rebalance-drawer-top-n"
            type="number"
            min={1}
            value={topNText}
            data-testid="drawer-top-n"
            onChange={(event) => onTopN(event.target.value)}
          />
        </div>
        <div className="w-28 space-y-1">
          <Label htmlFor="rebalance-drawer-hold-buffer">Hold buffer</Label>
          <Input
            id="rebalance-drawer-hold-buffer"
            type="number"
            min={0}
            value={holdBufferText}
            data-testid="drawer-hold-buffer"
            onChange={(event) => onHoldBuffer(event.target.value)}
          />
        </div>
        <Button
          variant="primary"
          size="sm"
          disabled={!canRun || analysing}
          data-testid="run-diff"
          onClick={onRun}
        >
          <Play aria-hidden="true" />
          {analysing ? "Comparing…" : "Compare with what you hold"}
        </Button>
        {canRun ? null : (
          /* PC1 shipped a primary action wired to a callback nobody passed, and it swallowed every
             click. A disabled control here always says, beside itself, what would enable it. */
          <p data-testid="cannot-run" className="w-full text-xs leading-snug text-muted-foreground">
            {screens.length === 0
              ? "There is no screen to compare against."
              : "This review was opened without a way to re-run the diff, so the controls above describe the proposal you are looking at rather than asking for a new one."}
          </p>
        )}
      </div>

      {analyseError !== null ? (
        <p
          data-testid="analyse-error"
          className="flex items-start gap-2 rounded-xl border border-negative/30 bg-negative-muted px-3 py-2.5 text-xs leading-snug text-negative"
        >
          <CircleAlert aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
          {analyseError}
        </p>
      ) : null}

      {preview.status === "ready" ? (
        <>
          <ListSummary lists={preview.lists} />
          <p className="text-xs leading-relaxed text-muted-foreground">
            The rank-buffer rule, in one line: buy the screen&rsquo;s top {preview.topN}, and keep a
            holding until its rank falls past {preview.bandLimit}. The buffer is what stops a name
            that slips one place from being sold and bought back next month.
          </p>
        </>
      ) : (
        <p data-testid="not-analysed-state" className="text-sm leading-relaxed text-muted-foreground">
          Nothing has been compared yet. Choose a screen and a hold buffer above, and the four
          lists — exits, entries, the hold band and your core holds — appear here with the weight
          each name carries now against the weight the screen is aiming at.
        </p>
      )}
    </div>
  );
}

function AdjustStep({
  preview,
  notes,
  onToggleExclude,
  onNote,
  onRestoreAll,
}: {
  preview: ReturnType<typeof rebalancePreview>;
  notes: ReadonlyMap<number, string>;
  onToggleExclude: (instrumentId: number) => void;
  onNote: (instrumentId: number, note: string) => void;
  onRestoreAll: () => void;
}) {
  const excludedCount = preview.excludedRows.length;
  return (
    <div className="space-y-3">
      <div
        className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-xl border border-border bg-card px-3 py-2.5 text-xs"
        data-testid="exclusion-summary"
      >
        {excludedCount === 0 ? (
          <span className="text-muted-foreground">
            Nothing is excluded. Every entry and exit below is in the plan.
          </span>
        ) : (
          <>
            <span className="font-medium">
              {excludedCount} name{excludedCount === 1 ? "" : "s"} excluded:{" "}
              <span className="font-normal text-muted-foreground">
                {preview.excludedRows.map((row) => row.symbol).join(", ")}
              </span>
            </span>
            <Button variant="ghost" size="sm" data-testid="restore-all" onClick={onRestoreAll}>
              Put them all back
            </Button>
          </>
        )}
      </div>

      <WeightComparison
        preview={preview}
        notes={notes}
        onToggleExclude={onToggleExclude}
        onNote={onNote}
      />
    </div>
  );
}
