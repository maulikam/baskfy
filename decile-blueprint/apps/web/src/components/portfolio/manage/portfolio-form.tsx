"use client";

import { useMemo, useState } from "react";
import { ArrowRight, Lock } from "lucide-react";

import { HoldingsPicker } from "@/components/portfolio/holdings-picker";
import { Field, Notice, PanelHeading } from "@/components/portfolio/manage/panel-chrome";
import { WriteFailure, useWrite } from "@/components/portfolio/manage/write-state";
import { PortfolioKindChoice } from "@/components/portfolio/portfolio-kind-choice";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import {
  actionById,
  successSentence,
  type ManageOutcome,
} from "@/lib/portfolio/manage";
import {
  DEFAULT_BENCHMARKS,
  parseHoldingKeyId,
  type AggregatedHolding,
  type PortfolioDraft,
  type PortfolioKind,
} from "@/lib/portfolio/organize";
import type { PortfolioRow } from "@/lib/portfolio/overview";

/**
 * The identity of a portfolio: what it is called, what it is measured against, and — once, at
 * the start — which of §4.1's two kinds it is.
 *
 * WHAT THIS PANEL REFUSES TO DRAW
 * -------------------------------
 * A benchmark select on an existing portfolio, and an objective box anywhere.
 *
 * `PortfolioPatchIn` carries `name`, `parent_id` and `broker_account_id`. There is no benchmark
 * field on it and no objective field on anything — not on the patch body, not on `NewPortfolioIn`,
 * not on the `portfolio` table. A benchmark select on the rename form would therefore be a
 * control that saves nothing, and an objective box would be a control with nowhere to save *to*.
 * Both are named instead, with the reason and what would unblock them, which is the same rule
 * that governs archive and permissions further down the drawer. The finding is written up in
 * `docs/pc-findings/pc6.md` because §2.2 did not know it.
 *
 * The kind choice is rendered in full, with §4.1's consequences, at the moment of choosing —
 * `PortfolioKindChoice` is the shipped control and it explains both answers in place rather than
 * behind a link. Choosing "Monitoring view" hands over to the view builder rather than quietly
 * changing what this form saves, because a lens is made from names and a capital portfolio from
 * shares, and one form pretending to do both is how the difference gets lost.
 */

export interface CreatePortfolioPanelProps {
  rows: readonly AggregatedHolding[];
  sectors?: Readonly<Record<string, string>> | undefined;
  /** Index names from `GET /meta/universes`. Falls back to the four broad NSE indices. */
  benchmarks?: readonly string[] | undefined;
  onCreate?: ((draft: PortfolioDraft) => Promise<ManageOutcome>) | undefined;
  onSaved: (message: string) => void;
  /** Hands over to the monitoring-view builder when the kind chosen is a lens. */
  onSwitchToViews: () => void;
}

export function CreatePortfolioPanel({
  rows,
  sectors,
  benchmarks,
  onCreate,
  onSaved,
  onSwitchToViews,
}: CreatePortfolioPanelProps) {
  const action = actionById("create");
  const benchmarkAction = actionById("benchmark");
  const write = useWrite();

  const [kind, setKind] = useState<PortfolioKind>("CAPITAL");
  const [name, setName] = useState("");
  const [benchmark, setBenchmark] = useState<string>(DEFAULT_BENCHMARKS[0] ?? "");
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [quantities, setQuantities] = useState<ReadonlyMap<string, string>>(new Map());

  const options = benchmarks && benchmarks.length > 0 ? benchmarks : DEFAULT_BENCHMARKS;
  const nameIsBlank = name.trim() === "";

  async function commit(): Promise<void> {
    const keys = [...selected]
      .map(parseHoldingKeyId)
      .filter((key): key is NonNullable<typeof key> => key !== null);
    const draft: PortfolioDraft = {
      start: keys.length === 0 ? "EMPTY" : "HOLDINGS",
      kind: "CAPITAL",
      name: name.trim(),
      benchmark,
      keys,
      quantities,
      sourceId: null,
      targetPortfolioId: null,
    };
    await write.run(
      action,
      onCreate === undefined ? undefined : () => onCreate(draft),
      () => {
        const saved = name.trim();
        setName("");
        setSelected(new Set());
        setQuantities(new Map());
        onSaved(successSentence(action, saved));
      },
    );
  }

  return (
    <div className="space-y-4" data-testid="create-portfolio-panel">
      <PanelHeading title={action.title}>{action.blurb}</PanelHeading>

      <PortfolioKindChoice value={kind} onChange={setKind} holdingCount={selected.size} />

      {kind === "MONITORING" ? (
        <div className="space-y-3" data-testid="create-hands-over-to-views">
          <Notice testId="create-view-notice">
            A monitoring view is built from names rather than share counts, and it reallocates
            nothing. It is made in the view builder, which shows what each name it watches is
            worth without adding any of it to your net worth.
          </Notice>
          <Button type="button" variant="primary" size="sm" onClick={onSwitchToViews}>
            Open the monitoring-view builder
            <ArrowRight aria-hidden="true" />
          </Button>
        </div>
      ) : (
        <>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Name" hint="What this portfolio is called on every screen.">
              {(id) => (
                <Input
                  id={id}
                  value={name}
                  placeholder="Long term"
                  onChange={(event) => {
                    setName(event.target.value);
                    write.clearFailure();
                  }}
                />
              )}
            </Field>

            <Field
              label="Benchmark"
              hint="Chosen now, and only now — see the note below. Its return is drawn beside this portfolio's."
            >
              {(id) => (
                <Select
                  id={id}
                  value={benchmark}
                  onChange={(event) => setBenchmark(event.target.value)}
                >
                  {options.map((option) => (
                    <option key={option} value={option}>
                      {option}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
          </div>

          {benchmarkAction.availability.kind === "create-only" ? (
            <Notice testId="benchmark-create-only">
              <strong className="font-medium text-foreground">
                A benchmark can be set here and not changed later.{" "}
              </strong>
              {benchmarkAction.availability.reason} Unblocked by:{" "}
              {benchmarkAction.availability.unblockedBy}
            </Notice>
          ) : null}

          <HoldingsPicker
            rows={rows}
            {...(sectors ? { sectors } : {})}
            selected={selected}
            onChange={(next) => {
              setSelected(next);
              write.clearFailure();
            }}
            quantities={quantities}
            onQuantitiesChange={(next) => {
              setQuantities(next);
              write.clearFailure();
            }}
            targetPortfolioId={null}
            portfolioName={name}
          />

          <WriteFailure failure={write.failure} />

          <div className="flex flex-wrap items-center gap-2 border-t border-border pt-3">
            <Button
              type="button"
              variant="primary"
              size="sm"
              disabled={nameIsBlank || write.saving || onCreate === undefined}
              onClick={() => void commit()}
            >
              {write.saving ? "Creating…" : "Create portfolio"}
            </Button>
            {nameIsBlank ? (
              <span className="text-xs text-muted-foreground">Give it a name first.</span>
            ) : onCreate === undefined ? (
              <span className="text-xs text-muted-foreground" data-testid="create-not-wired">
                This page has not passed a save handler yet, so nothing would be written. Your
                selection stays on screen.
              </span>
            ) : (
              <span className="text-xs text-muted-foreground">
                {selected.size === 0
                  ? "An empty portfolio is fine — you can file holdings into it afterwards."
                  : `${selected.size} holding${selected.size === 1 ? "" : "s"} would leave Unallocated.`}
              </span>
            )}
          </div>
        </>
      )}
    </div>
  );
}

export interface RenamePanelProps {
  portfolios: readonly PortfolioRow[];
  onRename?: ((portfolioId: number, name: string) => Promise<ManageOutcome>) | undefined;
  onSaved: (message: string) => void;
}

/**
 * Rename, and the two identity fields that cannot be edited afterwards, named in place.
 *
 * The absent controls are rendered *here*, on the form where a person would look for them, rather
 * than only in the drawer's "not available yet" list. Someone who opens Rename to change a
 * benchmark needs the answer at the point of the question; a list further down the drawer is
 * where they would look second.
 */
export function RenamePanel({ portfolios, onRename, onSaved }: RenamePanelProps) {
  const action = actionById("rename");
  const benchmarkAction = actionById("benchmark");
  const objectiveAction = actionById("objective");
  const write = useWrite();

  const [portfolioId, setPortfolioId] = useState<number | null>(
    portfolios[0]?.portfolio_id ?? null,
  );
  const chosen = useMemo(
    () => portfolios.find((row) => row.portfolio_id === portfolioId) ?? null,
    [portfolios, portfolioId],
  );
  const [name, setName] = useState(chosen?.name ?? "");
  const [lastChosen, setLastChosen] = useState<number | null>(portfolioId);

  /* Reset the box during render when the portfolio changes, rather than in an effect — the
     effect would paint the previous portfolio's name for one frame. Same trick as the shipped
     inspector drawer. */
  if (portfolioId !== lastChosen) {
    setLastChosen(portfolioId);
    setName(chosen?.name ?? "");
  }

  const trimmed = name.trim();
  const unchanged = chosen !== null && trimmed === chosen.name;

  async function commit(): Promise<void> {
    if (chosen === null || trimmed === "") return;
    await write.run(
      action,
      onRename === undefined ? undefined : () => onRename(chosen.portfolio_id, trimmed),
      () => onSaved(successSentence(action, trimmed)),
    );
  }

  return (
    <div className="space-y-4" data-testid="rename-panel">
      <PanelHeading title={action.title}>{action.blurb}</PanelHeading>

      {portfolios.length === 0 ? (
        <Notice tone="warning">There is no portfolio to rename yet.</Notice>
      ) : (
        <>
          <div className="grid gap-3 sm:grid-cols-2">
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
                  {portfolios.map((row) => (
                    <option key={row.portfolio_id} value={String(row.portfolio_id)}>
                      {row.name}
                      {row.kind === "MONITORING" ? " (view)" : ""}
                    </option>
                  ))}
                </Select>
              )}
            </Field>

            <Field label="New name" hint="Nothing else changes — not its holdings, not its history.">
              {(id) => (
                <Input
                  id={id}
                  value={name}
                  onChange={(event) => {
                    setName(event.target.value);
                    write.clearFailure();
                  }}
                />
              )}
            </Field>
          </div>

          {/* The two identity fields that have no write. Named where they would be looked for,
              with the reason and the unblock — never drawn as a control that saves nothing. */}
          <ul className="divide-y divide-border/60 rounded-lg border border-border bg-muted/40 px-3">
            {[benchmarkAction, objectiveAction].map((absent) => {
              const availability = absent.availability;
              const reason =
                availability.kind === "available" ? null : availability.reason;
              const unblockedBy =
                availability.kind === "available" ? null : availability.unblockedBy;
              if (reason === null || unblockedBy === null) return null;
              return (
                <li
                  key={absent.id}
                  data-testid={`rename-absent-${absent.id}`}
                  className="flex gap-2 py-2.5"
                >
                  <Lock aria-hidden="true" className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />
                  <div className="space-y-0.5 text-xs leading-relaxed text-muted-foreground">
                    <p className="font-medium text-foreground">
                      {absent.title}
                      <span className="ml-2 font-normal uppercase tracking-wide">
                        {availability.kind === "create-only"
                          ? "Set at creation only"
                          : "Not available yet"}
                      </span>
                    </p>
                    <p>{reason}</p>
                    <p>
                      <span className="font-medium text-foreground">What would unblock it: </span>
                      {unblockedBy}
                    </p>
                  </div>
                </li>
              );
            })}
          </ul>

          <WriteFailure failure={write.failure} />

          <div className="flex flex-wrap items-center gap-2 border-t border-border pt-3">
            <Button
              type="button"
              variant="primary"
              size="sm"
              disabled={chosen === null || trimmed === "" || unchanged || write.saving || onRename === undefined}
              onClick={() => void commit()}
            >
              {write.saving ? "Saving…" : "Rename"}
            </Button>
            {trimmed === "" ? (
              <span className="text-xs text-muted-foreground">A portfolio needs a name.</span>
            ) : unchanged ? (
              <span className="text-xs text-muted-foreground">
                That is already its name, so there is nothing to save.
              </span>
            ) : onRename === undefined ? (
              <span className="text-xs text-muted-foreground" data-testid="rename-not-wired">
                This page has not passed a save handler yet, so nothing would be written.
              </span>
            ) : null}
          </div>
        </>
      )}
    </div>
  );
}
