"use client";

import { useMemo, useState } from "react";

import { HoldingsPicker } from "@/components/portfolio/holdings-picker";
import { PortfolioKindChoice } from "@/components/portfolio/portfolio-kind-choice";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  DEFAULT_BENCHMARKS,
  KIND_EXPLAINER,
  MONITORING_NOTE,
  NO_FIGURE,
  parseHoldingKeyId,
  selectionTotal,
  type AggregatedHolding,
  type NewPortfolioStart,
  type PortfolioDraft,
  type PortfolioKind,
} from "@/lib/portfolio/organize";
import { formatRupees } from "@/lib/portfolios/decimal";
import { cn } from "@/lib/utils";

/**
 * PORTFOLIO_REDESIGN.md §6.7: `+ New portfolio` → pick a starting point → (for broker holdings)
 * the two-panel picker → choose kind → name → benchmark → confirm.
 *
 * Rendered **inline**, not in a modal. Getting from forty unallocated holdings to four named
 * portfolios (§6.6) is the opposite of a quick confirmation: it is the longest deliberate thing a
 * new user does, it wants the full width for two panels, and a dialog that traps focus over the
 * holdings the user is trying to read is fighting the task.
 *
 * The five starting points are §6.7's, in its order. The three that come from a model, a screen or
 * a strategy have nothing to pick from until those exist, and they say so in that case rather than
 * showing an empty list — and, per §1 problem 6, none of them sends the user to the catalog.
 *
 * The flow ends in a review, and confirm is only live when the caller passed an `onCreate`.
 * Writing a portfolio needs the allocation write API that §10 Phase 1 still owes; a button that
 * appears to save and does not is worse than one that says why it cannot.
 */

type Step = "start" | "source" | "target" | "holdings" | "kind" | "details" | "review";

export interface SourceOption {
  id: string;
  name: string;
  publisher?: string | null;
}

/** What an accepted §6.6 suggestion pre-fills. */
export interface NewPortfolioSeed {
  start: NewPortfolioStart;
  step?: Step;
  name?: string;
  kind?: PortfolioKind;
  selected?: readonly string[];
  sourceId?: string | null;
}

/** What `onCreate` may answer. Mirrors `lib/portfolio/create.CreateResult`, restated here so a
 *  client component does not import a `server-only` module. */
export type CreateOutcome =
  | { readonly ok: true; readonly portfolioId: number }
  | { readonly ok: false; readonly reason: string };

/** One portfolio the flow may file holdings INTO. Piles are excluded by the caller. */
export interface TargetPortfolio {
  portfolio_id: number;
  name: string;
  kind: PortfolioKind;
}

export interface NewPortfolioFlowProps {
  rows: readonly AggregatedHolding[];
  /** Existing portfolios, for the "add to one of these" path. Empty hides that option. */
  targets?: readonly TargetPortfolio[] | undefined;
  sectors?: Readonly<Record<string, string>> | undefined;
  seed?: NewPortfolioSeed | null | undefined;
  subscribedBaskets?: readonly SourceOption[] | undefined;
  screens?: readonly SourceOption[] | undefined;
  strategies?: readonly SourceOption[] | undefined;
  benchmarks?: readonly string[] | undefined;
  onCancel: () => void;
  /**
   * The write. Returns the server's answer so a refusal can be shown where the user is looking —
   * criterion 2's conflict ("this stock is already in another portfolio") is the case that
   * matters, and it arrives as a sentence naming the stock and the portfolio.
   */
  onCreate?: ((draft: PortfolioDraft) => Promise<CreateOutcome>) | undefined;
  /** Called after a successful create, so the host can close the flow and refresh. */
  onCreated?: ((portfolioId: number) => void) | undefined;
}

const START_OPTIONS: ReadonlyArray<{
  start: NewPortfolioStart;
  title: string;
  blurb: string;
}> = [
  {
    start: "SUBSCRIBED",
    title: "From a subscribed basket",
    blurb: "Track a model published by someone else against what you actually hold.",
  },
  {
    start: "MY_SCREEN",
    title: "From my screen",
    blurb: "Start from the stocks one of your screens ranks today.",
  },
  {
    start: "MY_STRATEGY",
    title: "From my strategy",
    blurb: "Driven by your own entry, rebalance and exit rules.",
  },
  {
    start: "HOLDINGS",
    title: "From broker holdings",
    blurb: "Pick shares you already own and group them. This is the one that empties Unallocated.",
  },
  {
    start: "EMPTY",
    title: "Empty",
    blurb: "A named, empty portfolio to fill in later.",
  },
  {
    start: "EXISTING",
    title: "Add to a portfolio you already have",
    blurb: "File more shares into one of your portfolios, moving them out of wherever they are.",
  },
];

const SOURCE_COPY: Record<
  "SUBSCRIBED" | "MY_SCREEN" | "MY_STRATEGY",
  { heading: string; empty: string }
> = {
  SUBSCRIBED: {
    heading: "Which model?",
    empty:
      "You do not subscribe to any published model yet, so there is nothing to build from. Group holdings you already own instead — that is the quicker start.",
  },
  MY_SCREEN: {
    heading: "Which screen?",
    empty:
      "You have not saved a screen yet. Group holdings you already own instead, and come back when a screen exists.",
  },
  MY_STRATEGY: {
    heading: "Which strategy?",
    empty:
      "You have not saved a strategy yet. Group holdings you already own instead, and come back when a strategy exists.",
  },
};

export function NewPortfolioFlow({
  rows,
  targets = [],
  sectors = {},
  seed = null,
  subscribedBaskets = [],
  screens = [],
  strategies = [],
  benchmarks = DEFAULT_BENCHMARKS,
  onCancel,
  onCreate,
  onCreated,
}: NewPortfolioFlowProps) {
  const [start, setStart] = useState<NewPortfolioStart | null>(seed?.start ?? null);
  //: The server's own sentence when a create is refused, and null while it has not been.
  const [refusal, setRefusal] = useState<string | null>(null);
  //: Guards a double submit — the confirm button is a network call now, not a state change.
  const [saving, setSaving] = useState(false);
  const [step, setStep] = useState<Step>(seed?.step ?? (seed ? "holdings" : "start"));
  const [selected, setSelected] = useState<Set<string>>(() => new Set(seed?.selected ?? []));
  /** `holdingKeyId -> shares`, only for the legs the user narrowed. Blank means "all of it". */
  const [quantities, setQuantities] = useState<Map<string, string>>(() => new Map());
  const [kind, setKind] = useState<PortfolioKind>(seed?.kind ?? "CAPITAL");
  const [name, setName] = useState(seed?.name ?? "");
  const [benchmark, setBenchmark] = useState(benchmarks[0] ?? "");
  const [sourceId, setSourceId] = useState<string | null>(seed?.sourceId ?? null);
  /**
   * The existing portfolio being filed into, when `start` is EXISTING.
   *
   * Pre-selected to the first target when the flow is OPENED on that path — the page's own
   * "Add to a portfolio" button seeds `start: "EXISTING"`, and a radio list arriving with nothing
   * chosen would leave Continue disabled for no reason a reader can see.
   */
  const [targetId, setTargetId] = useState<number | null>(
    seed?.start === "EXISTING" ? (targets[0]?.portfolio_id ?? null) : null,
  );

  /** EXISTING skips kind, name and benchmark — the portfolio already has all three. */
  const filingIntoExisting = start === "EXISTING";
  const target = targets.find((option) => option.portfolio_id === targetId) ?? null;

  const total = useMemo(
    () => selectionTotal(rows, selected, quantities, filingIntoExisting ? targetId : null),
    [rows, selected, quantities, filingIntoExisting, targetId],
  );

  const sourceOptions: readonly SourceOption[] =
    start === "SUBSCRIBED" ? subscribedBaskets : start === "MY_SCREEN" ? screens : strategies;

  function chooseStart(next: NewPortfolioStart): void {
    setStart(next);
    if (next === "EXISTING") {
      setTargetId(targets[0]?.portfolio_id ?? null);
      setStep("target");
      return;
    }
    setStep(next === "HOLDINGS" ? "holdings" : next === "EMPTY" ? "kind" : "source");
  }


  function back(): void {
    if (step === "review") setStep("details");
    else if (step === "details") setStep("kind");
    else if (step === "kind") setStep(start === "HOLDINGS" ? "holdings" : start === "EMPTY" ? "start" : "source");
    else if (step === "holdings" && filingIntoExisting) setStep("target");
    else setStep("start");
  }

  async function confirm(): Promise<void> {
    if (!onCreate || start === null || saving) return;
    const keys = [...selected]
      .map(parseHoldingKeyId)
      .filter((key): key is NonNullable<typeof key> => key !== null);
    setSaving(true);
    setRefusal(null);
    try {
      const outcome = await onCreate({
        start,
        kind,
        name: name.trim(),
        benchmark,
        keys,
        // A lens takes no quantities (§4.1): it answers "which names", not "how many". Sending
        // them would be sending a fact the server has nowhere to put.
        ...(kind === "MONITORING" ? {} : { quantities }),
        sourceId,
        targetPortfolioId: filingIntoExisting ? targetId : null,
      });
      // A handler that resolves to nothing is a spy in a test that only cares the click fired;
      // treating that as a refusal would put an error on screen for a create that worked.
      if (!outcome) return;
      if (outcome.ok) {
        onCreated?.(outcome.portfolioId);
        return;
      }
      setRefusal(outcome.reason);
    } finally {
      setSaving(false);
    }
  }

  const nameIsBlank = name.trim() === "";

  return (
    <section
      aria-label="New portfolio"
      data-testid="new-portfolio-flow"
      data-step={step}
      className="space-y-4 rounded-xl border border-border bg-card p-4 sm:p-5"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-base font-semibold">New portfolio</h2>
        <Button type="button" variant="ghost" size="sm" onClick={onCancel}>
          Cancel
        </Button>
      </div>

      {/* ------------------------------------------------------------- start */}
      {step === "start" ? (
        <ul className="grid gap-2 sm:grid-cols-2" data-testid="start-options">
          {/* "Add to a portfolio you already have" is hidden until there is one. An option that
              leads to an empty list is a dead end dressed as a choice. */}
          {START_OPTIONS.filter(
            (option) => option.start !== "EXISTING" || targets.length > 0,
          ).map((option) => (
            <li key={option.start}>
              <button
                type="button"
                onClick={() => chooseStart(option.start)}
                className="w-full rounded-xl border border-border bg-background p-4 text-left transition-colors hover:border-foreground/60"
              >
                <span className="block text-sm font-semibold">{option.title}</span>
                <span className="mt-1 block text-xs text-muted-foreground">{option.blurb}</span>
              </button>
            </li>
          ))}
        </ul>
      ) : null}

      {/* ------------------------------------------------------------ source */}
      {step === "source" &&
      start !== null &&
      start !== "HOLDINGS" &&
      start !== "EMPTY" &&
      start !== "EXISTING" ? (
        <div className="space-y-3" data-testid="source-step">
          <h3 className="text-sm font-semibold">{SOURCE_COPY[start].heading}</h3>
          {sourceOptions.length === 0 ? (
            <div className="space-y-3 rounded-lg border border-dashed border-border px-3 py-4">
              <p className="text-sm text-muted-foreground">{SOURCE_COPY[start].empty}</p>
              <Button type="button" variant="outline" size="sm" onClick={() => chooseStart("HOLDINGS")}>
                Group holdings I already own
              </Button>
            </div>
          ) : (
            <ul className="space-y-2">
              {sourceOptions.map((option) => (
                <li key={option.id}>
                  <label className="flex cursor-pointer items-center gap-2 rounded-lg border border-border p-3 text-sm">
                    <input
                      type="radio"
                      name="portfolio-source"
                      checked={sourceId === option.id}
                      onChange={() => setSourceId(option.id)}
                      className="size-4"
                    />
                    <span>
                      {option.name}
                      {option.publisher ? (
                        <span className="block text-xs text-muted-foreground">
                          Subscribed model by {option.publisher}
                        </span>
                      ) : null}
                    </span>
                  </label>
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : null}

      {/* ---------------------------------------------------------- holdings */}
      {/* ------------------------------------------------------------- target */}
      {step === "target" ? (
        <div className="space-y-3" data-testid="target-step">
          <div>
            <h3 className="text-sm font-semibold">Which portfolio?</h3>
            <p className="text-xs text-muted-foreground">
              Shares you pick next are filed into it. Anything already in another portfolio moves
              across; unallocated shares are taken first.
            </p>
          </div>
          {targets.length === 0 ? (
            <p className="rounded-lg border border-dashed border-border px-3 py-6 text-center text-sm text-muted-foreground">
              You have no portfolios to add to yet. Create one first.
            </p>
          ) : (
            <ul className="space-y-1">
              {targets.map((option) => (
                <li key={option.portfolio_id}>
                  <label className="flex cursor-pointer items-center gap-2 rounded-lg border border-border px-3 py-2 text-sm hover:bg-muted/40">
                    <input
                      type="radio"
                      name="target-portfolio"
                      checked={targetId === option.portfolio_id}
                      onChange={() => setTargetId(option.portfolio_id)}
                      className="size-4"
                    />
                    <span className="flex-1 truncate">{option.name}</span>
                    {option.kind === "MONITORING" ? (
                      <span className="text-xs text-muted-foreground">watchlist — no quantities</span>
                    ) : null}
                  </label>
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : null}

      {step === "holdings" ? (
        <HoldingsPicker
          rows={rows}
          sectors={sectors}
          selected={selected}
          onChange={setSelected}
          quantities={quantities}
          onQuantitiesChange={setQuantities}
          targetPortfolioId={filingIntoExisting ? targetId : null}
          portfolioName={name}
        />
      ) : null}

      {/* -------------------------------------------------------------- kind */}
      {step === "kind" ? (
        <PortfolioKindChoice value={kind} onChange={setKind} holdingCount={total.holdings} />
      ) : null}

      {/* ----------------------------------------------------------- details */}
      {step === "details" ? (
        <div className="space-y-4" data-testid="details-step">
          <div className="flex flex-col gap-1 text-sm">
            <label htmlFor="new-portfolio-name" className="font-semibold">
              Name
            </label>
            <input
              id="new-portfolio-name"
              type="text"
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="Long-term financials"
              className="h-9 rounded-md border border-input bg-background px-3 text-sm"
            />
            <p className="text-xs text-muted-foreground">
              What you will call this on the Overview table.
            </p>
          </div>

          <div className="flex flex-col gap-1 text-sm">
            <label htmlFor="new-portfolio-benchmark" className="font-semibold">
              Benchmark
            </label>
            <select
              id="new-portfolio-benchmark"
              value={benchmark}
              onChange={(event) => setBenchmark(event.target.value)}
              className="h-9 rounded-md border border-input bg-background px-2 text-sm"
            >
              {benchmarks.map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </select>
            <p className="text-xs text-muted-foreground">
              What this portfolio is compared against on its chart. Change it later at any time.
            </p>
          </div>
        </div>
      ) : null}

      {/* ------------------------------------------------------------ review */}
      {step === "review" ? (
        <div className="space-y-3" data-testid="review-step">
          <h3 className="text-sm font-semibold">Check this over</h3>
          <dl className="grid gap-2 text-sm sm:grid-cols-2">
            <div>
              <dt className="text-xs uppercase tracking-wide text-muted-foreground">Name</dt>
              <dd>{nameIsBlank ? NO_FIGURE : name.trim()}</dd>
            </div>
            <div>
              <dt className="text-xs uppercase tracking-wide text-muted-foreground">Kind</dt>
              <dd className={cn(kind === "MONITORING" && "text-muted-foreground")}>
                {KIND_EXPLAINER[kind].title}
              </dd>
            </div>
            <div>
              <dt className="text-xs uppercase tracking-wide text-muted-foreground">Holdings</dt>
              <dd>
                {total.holdings} holding{total.holdings === 1 ? "" : "s"} ·{" "}
                {total.holdings === 0 ? NO_FIGURE : formatRupees(total.value)}
              </dd>
            </div>
            <div>
              <dt className="text-xs uppercase tracking-wide text-muted-foreground">Benchmark</dt>
              <dd>{benchmark === "" ? NO_FIGURE : benchmark}</dd>
            </div>
          </dl>

          {kind === "MONITORING" ? (
            <Badge variant="neutral" data-testid="review-monitoring-note">
              {MONITORING_NOTE}
            </Badge>
          ) : null}

          {onCreate ? null : (
            <p className="rounded-lg border border-dashed border-border px-3 py-3 text-xs text-muted-foreground">
              This preview does not save. The page that hosts this flow passes an{" "}
              <code>onCreate</code> handler; until it does, nothing is lost — this selection stays
              on screen.
            </p>
          )}

          {refusal ? (
            <p
              role="alert"
              data-testid="create-refusal"
              className="rounded-lg border border-destructive/40 bg-destructive/5 px-3 py-3 text-xs text-foreground"
            >
              {refusal}
            </p>
          ) : null}
        </div>
      ) : null}

      {/* A refusal is shown WHERE THE USER IS. It used to render only inside the review step, so
          an add-to-existing refusal — which is confirmed from the holdings step — was set into
          state and never drawn: the button looked broken rather than refused. */}
      {refusal && step !== "review" ? (
        <p
          role="alert"
          data-testid="flow-refusal"
          className="rounded-lg border border-destructive/40 bg-destructive/5 px-3 py-3 text-xs text-foreground"
        >
          {refusal}
        </p>
      ) : null}

      {/* -------------------------------------------------------------- foot */}
      {step === "start" ? null : (
        <div className="flex flex-wrap items-center gap-2 border-t border-border pt-3">
          <Button type="button" variant="outline" size="sm" onClick={back}>
            Back
          </Button>

          {step === "target" ? (
            <Button
              type="button"
              variant="primary"
              size="sm"
              disabled={targetId === null}
              onClick={() => setStep("holdings")}
            >
              Continue
            </Button>
          ) : null}

          {step === "holdings" ? (
            <>
              <Button
                type="button"
                variant="primary"
                size="sm"
                disabled={total.holdings === 0 || saving}
                onClick={() => (filingIntoExisting ? void confirm() : setStep("kind"))}
              >
                {filingIntoExisting
                  ? saving
                    ? "Adding…"
                    : `Add to ${target?.name ?? "portfolio"}`
                  : "Continue"}
              </Button>
              {total.holdings === 0 ? (
                <span className="text-xs text-muted-foreground">Pick at least one holding.</span>
              ) : (
                <span className="text-xs text-muted-foreground">
                  {total.holdings} selected · {formatRupees(total.value)}
                </span>
              )}
            </>
          ) : null}

          {step === "source" ? (
            <Button
              type="button"
              variant="primary"
              size="sm"
              disabled={sourceId === null}
              onClick={() => setStep("kind")}
            >
              Continue
            </Button>
          ) : null}

          {step === "kind" ? (
            <Button type="button" variant="primary" size="sm" onClick={() => setStep("details")}>
              Continue
            </Button>
          ) : null}

          {step === "details" ? (
            <>
              <Button
                type="button"
                variant="primary"
                size="sm"
                disabled={nameIsBlank}
                onClick={() => setStep("review")}
              >
                Continue
              </Button>
              {nameIsBlank ? (
                <span className="text-xs text-muted-foreground">Give it a name first.</span>
              ) : null}
            </>
          ) : null}

          {step === "review" ? (
            <Button
              type="button"
              variant="primary"
              size="sm"
              disabled={!onCreate || nameIsBlank || saving}
              // `void` because the handler is async and a click handler returns nothing;
              // eslint's no-misused-promises is right to want that said explicitly.
              onClick={() => void confirm()}
            >
              {saving ? "Creating…" : "Create portfolio"}
            </Button>
          ) : null}
        </div>
      )}
    </section>
  );
}
