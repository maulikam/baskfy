"use client";

import { useEffect, useState } from "react";

import { Field, Notice, PanelHeading } from "@/components/portfolio/manage/panel-chrome";
import { WriteFailure, useWrite } from "@/components/portfolio/manage/write-state";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import {
  actionById,
  successSentence,
  type ManageOutcome,
  type SleeveDraft,
  type SleeveRow,
} from "@/lib/portfolio/manage";
import type { PortfolioRow } from "@/lib/portfolio/overview";
import { addDecimalStrings, formatRupees } from "@/lib/portfolios/decimal";

/**
 * Whole rupees, matching the shipped allocation planner.
 *
 * `numeric(18,2)` serialises with its paise and a sleeve's capital rendered with them reads like
 * an accounting entry rather than a plan. Whole rupees is the precision the allocator actually
 * uses, and the two screens showing the same sleeve two different ways is how a reader starts
 * wondering which one is the real figure.
 */
function wholeRupees(value: string | null | undefined): string {
  return formatRupees(value, { decimals: 0 });
}

/** What drives an allocation, in the words §8 renamed it to. Pulled out of the JSX because a
 *  nested template literal reads as one string to the jargon scanner, and to a reviewer. */
function describeKind(sleeve: SleeveRow): string {
  if (sleeve.kind === "manual") return "set by hand";
  const named = sleeve.screen_name;
  return named === null || named === undefined ? "from a screen" : `from your screen · ${named}`;
}

/**
 * Sub-portfolios — sleeves. A portfolio's capital, divided into named parts.
 *
 * THE ONE THING THIS FORM MUST NOT DO
 * -----------------------------------
 * `PUT /portfolios/{id}/sleeves` **replaces the whole set**, deliberately: a portfolio's division
 * is one decision and the slices have to add up to something the owner meant, so patching sleeve
 * two while sleeve three still holds last week's capital is how a total quietly stops being the
 * portfolio.
 *
 * That makes the read a *precondition of the write*. A form that lets you add "Momentum — 4,00,000"
 * without having loaded the three sleeves already there does not add a sleeve; it deletes three.
 * So the save button is unreachable until the current division has actually been read back, and a
 * failed read says so in place of enabling a button that would destroy something. This is the one
 * place in the drawer where *not* being able to do a thing is the safe outcome, and it is enforced
 * by the state rather than by the user noticing an empty list.
 *
 * WHAT THIS FORM DELIBERATELY DOES NOT OFFER
 * ------------------------------------------
 * A screen-driven sleeve. Those carry a screen id and a `top_n`, and choosing them properly means
 * the screen catalogue, a rank buffer and the allocation preview — which is the full planner at
 * `/portfolios/{id}/sleeves`, already built. The drawer adds a named manual slice and links to the
 * planner for the rest, rather than shipping half a screen picker.
 */

export interface SleeveFormProps {
  portfolios: readonly PortfolioRow[];
  /** Reads the current division. Required before any save — see the module docstring. */
  loadSleeves?: ((portfolioId: number) => Promise<readonly SleeveRow[]>) | undefined;
  onSaveSleeves?:
    | ((portfolioId: number, sleeves: readonly SleeveDraft[]) => Promise<ManageOutcome>)
    | undefined;
  onSaved: (message: string) => void;
}

/** What a finished read produced. Held per portfolio, so switching back does not refetch. */
type LoadResult =
  | { readonly status: "loaded"; readonly sleeves: readonly SleeveRow[] }
  | { readonly status: "failed"; readonly reason: string };

type LoadState = LoadResult | { readonly status: "idle" } | { readonly status: "loading" };

export function SleeveForm({
  portfolios,
  loadSleeves,
  onSaveSleeves,
  onSaved,
}: SleeveFormProps) {
  const action = actionById("sleeve");
  const write = useWrite();

  const capital = portfolios.filter((row) => row.kind === "CAPITAL");
  const [portfolioId, setPortfolioId] = useState<number | null>(capital[0]?.portfolio_id ?? null);
  const [results, setResults] = useState<ReadonlyMap<number, LoadResult>>(new Map());
  const [name, setName] = useState("");
  const [amount, setAmount] = useState("");

  const chosen = capital.find((row) => row.portfolio_id === portfolioId) ?? null;

  /* The read is the only thing that reaches outside React here, and its result lands in an async
     callback rather than in the effect body — a synchronous `setState` in an effect is a
     cascading render, and the lint rule that says so is right. "Loading" is therefore *derived*
     below (a chosen portfolio with no result yet) rather than written into state. */
  useEffect(() => {
    if (portfolioId === null || loadSleeves === undefined) return;
    if (results.has(portfolioId)) return;
    let live = true;
    const remember = (result: LoadResult): void => {
      if (!live) return;
      setResults((previous) => new Map(previous).set(portfolioId, result));
    };
    void loadSleeves(portfolioId)
      .then((sleeves) => remember({ status: "loaded", sleeves }))
      .catch((error: unknown) =>
        remember({
          status: "failed",
          reason:
            error instanceof Error && error.message.trim() !== ""
              ? error.message
              : "The current division could not be read.",
        }),
      );
    return () => {
      live = false;
    };
  }, [portfolioId, loadSleeves, results]);

  const state: LoadState =
    portfolioId === null || loadSleeves === undefined
      ? { status: "idle" }
      : (results.get(portfolioId) ?? { status: "loading" });

  const existing = state.status === "loaded" ? state.sleeves : [];
  const existingTotal = addDecimalStrings(existing.map((sleeve) => sleeve.capital));
  const nextTotal = addDecimalStrings([existingTotal, amount.trim() === "" ? "0" : amount.trim()]);

  const nameIsBlank = name.trim() === "";
  const amountIsBlank = amount.trim() === "";
  const canSave =
    state.status === "loaded" &&
    chosen !== null &&
    !nameIsBlank &&
    !amountIsBlank &&
    onSaveSleeves !== undefined;

  async function commit(): Promise<void> {
    if (state.status !== "loaded" || chosen === null) return;
    const body: SleeveDraft[] = [
      ...state.sleeves.map((sleeve) => ({
        name: sleeve.name,
        kind: sleeve.kind,
        capital: sleeve.capital,
        basket_slug: sleeve.basket_slug ?? null,
        screen_public_id: sleeve.screen_public_id ?? null,
        top_n: sleeve.top_n,
      })),
      {
        name: name.trim(),
        /* Manual: a named slice of capital with no screen behind it. The screen-driven kind is
           the planner's, for the reason in the module docstring. */
        kind: "manual",
        capital: amount.trim(),
        basket_slug: null,
        screen_public_id: null,
        top_n: 15,
      },
    ];
    await write.run(
      action,
      onSaveSleeves === undefined ? undefined : () => onSaveSleeves(chosen.portfolio_id, body),
      () => {
        setName("");
        setAmount("");
        /* Forget what we read, so the effect reads the division back rather than showing the
           set from before the save as though it were current. */
        setResults((previous) => {
          const next = new Map(previous);
          next.delete(chosen.portfolio_id);
          return next;
        });
        onSaved(successSentence(action, chosen.name));
      },
    );
  }

  return (
    <div className="space-y-4" data-testid="sleeve-form" data-load={state.status}>
      <PanelHeading title={action.title}>{action.blurb}</PanelHeading>

      {capital.length === 0 ? (
        <Notice tone="warning">
          There is no capital portfolio to split up yet. A monitoring view owns no capital, so it
          has no allocations to make.
        </Notice>
      ) : (
        <>
          <Field label="Portfolio">
            {(id) => (
              <Select
                id={id}
                value={portfolioId === null ? "" : String(portfolioId)}
                onChange={(event) => {
                  setPortfolioId(event.target.value === "" ? null : Number(event.target.value));
                  write.clearFailure();
                }}
              >
                {capital.map((row) => (
                  <option key={row.portfolio_id} value={String(row.portfolio_id)}>
                    {row.name}
                  </option>
                ))}
              </Select>
            )}
          </Field>

          {state.status === "idle" && loadSleeves === undefined ? (
            <Notice tone="warning" testId="sleeve-not-wired">
              This page has not passed a reader for the current split. Saving would replace the
              whole set, so adding one allocation without first reading what is there would delete
              the allocations already saved — the button stays off until the page wires it.
            </Notice>
          ) : null}

          {state.status === "loading" ? (
            <p className="text-xs text-muted-foreground" data-testid="sleeve-loading">
              Reading the current split…
            </p>
          ) : null}

          {state.status === "failed" ? (
            <Notice tone="warning" testId="sleeve-load-failed">
              The current split of {chosen?.name ?? "this portfolio"} could not be read, so
              nothing can be saved: this endpoint replaces the whole set, and saving now would
              delete whatever is already there. {state.reason}
            </Notice>
          ) : null}

          {state.status === "loaded" ? (
            <section
              aria-label="How this portfolio is split up"
              data-testid="sleeve-current"
              className="space-y-2 rounded-xl border border-border bg-card p-3"
            >
              <h4 className="text-sm font-semibold">
                {chosen?.name ?? "This portfolio"} is split into {existing.length} allocation
                {existing.length === 1 ? "" : "s"}
              </h4>
              {existing.length === 0 ? (
                <p className="text-xs text-muted-foreground">
                  Nothing is split up yet. The allocation you add below will be the first.
                </p>
              ) : (
                <ul className="divide-y divide-border/60 text-xs">
                  {existing.map((sleeve) => (
                    <li key={sleeve.id} className="flex items-baseline justify-between gap-2 py-1.5">
                      <span className="min-w-0 truncate">
                        {sleeve.name}
                        <span className="ml-1.5 text-muted-foreground">{describeKind(sleeve)}</span>
                      </span>
                      <span className="shrink-0 tabular-nums">{wholeRupees(sleeve.capital)}</span>
                    </li>
                  ))}
                </ul>
              )}
              <p className="text-xs text-muted-foreground" data-testid="sleeve-replace-note">
                Saving replaces this whole split in one write. The {existing.length} allocation
                {existing.length === 1 ? "" : "s"} above {existing.length === 1 ? "is" : "are"} sent
                back unchanged alongside the new one.
              </p>
            </section>
          ) : null}

          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="New allocation" hint="What this part of the portfolio is for.">
              {(id) => (
                <Input
                  id={id}
                  value={name}
                  placeholder="Momentum"
                  onChange={(event) => {
                    setName(event.target.value);
                    write.clearFailure();
                  }}
                />
              )}
            </Field>
            <Field
              label="Capital"
              hint="Rupees. This is a plan for how the portfolio is split up — no order is placed."
            >
              {(id) => (
                <Input
                  id={id}
                  inputMode="decimal"
                  value={amount}
                  placeholder="400000"
                  onChange={(event) => {
                    setAmount(event.target.value);
                    write.clearFailure();
                  }}
                />
              )}
            </Field>
          </div>

          {state.status === "loaded" ? (
            <p className="text-xs text-muted-foreground" data-testid="sleeve-preview">
              Capital given out goes from {wholeRupees(existingTotal)} to {wholeRupees(nextTotal)}.
            </p>
          ) : null}

          <p className="text-xs text-muted-foreground">
            An allocation driven by one of your screens, with a rank buffer and a preview of what
            it would hold, is set up in the full planner at{" "}
            <code>/portfolios/{portfolioId ?? 0}/sleeves</code>.
          </p>

          <WriteFailure failure={write.failure} />

          <div className="flex flex-wrap items-center gap-2 border-t border-border pt-3">
            <Button
              type="button"
              variant="primary"
              size="sm"
              disabled={!canSave || write.saving}
              onClick={() => void commit()}
            >
              {write.saving ? "Saving…" : "Add this allocation"}
            </Button>
            {canSave ? null : (
              <span className="text-xs text-muted-foreground" data-testid="sleeve-blocked-reason">
                {state.status !== "loaded"
                  ? "The current split has to be read before it can be replaced."
                  : nameIsBlank
                    ? "Name the allocation first."
                    : amountIsBlank
                      ? "Say how much capital this allocation gets."
                      : "This page has not passed a save handler for allocations yet."}
              </span>
            )}
          </div>
        </>
      )}
    </div>
  );
}
