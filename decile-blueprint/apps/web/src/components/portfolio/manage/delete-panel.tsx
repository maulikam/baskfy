"use client";

import { useMemo, useState } from "react";
import { CircleAlert, Trash2 } from "lucide-react";

import { Field, MetricLine, Notice, PanelHeading } from "@/components/portfolio/manage/panel-chrome";
import { WriteFailure, useWrite } from "@/components/portfolio/manage/write-state";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import {
  actionById,
  deleteImpact,
  successSentence,
  type ManageOutcome,
} from "@/lib/portfolio/manage";
import type { PortfolioRow } from "@/lib/portfolio/overview";

/**
 * Delete, with what is lost named — and what is *not* lost named just as loudly.
 *
 * THE FEAR THIS PANEL HAS TO ANSWER FIRST
 * ---------------------------------------
 * "Will this sell my shares?" It will not, and it cannot: the row being deleted is bookkeeping,
 * `portfolio_holding.portfolio_id` is `ON DELETE CASCADE`, and nothing on this path reaches a
 * broker. The shares stay in the demat account and become unallocated. That sentence is first,
 * before the warning, because a person who is not sure of it will either not press the button or
 * press it and be frightened.
 *
 * THE COST NOBODY EXPECTS, WHICH IS THEREFORE SECOND
 * --------------------------------------------------
 * `portfolio_nav_daily` and `portfolio_cash_flow` both cascade too, and §5.1 is explicit that the
 * NAV series is **stored, not recomputed** — it is a record of a claim about a day, not a
 * derivation from today's holdings. So deleting a portfolio destroys its chart, its drawdown and
 * the dated flows XIRR is solved from, permanently. Recreating it tomorrow with the same holdings
 * starts its history at tomorrow. A confirmation that says "this cannot be undone" and leaves the
 * reader to guess what "this" is has not warned them of that.
 *
 * The confirmation is the portfolio's own name, typed. Not because typing is a ritual, but
 * because the select above it lists every portfolio and the one thing a destructive action must
 * not do is act on the wrong row.
 */

export interface DeletePanelProps {
  /** Capital portfolios and monitoring views together — both can be deleted. */
  portfolios: readonly PortfolioRow[];
  onDelete?: ((portfolioId: number) => Promise<ManageOutcome>) | undefined;
  onSaved: (message: string) => void;
}

export function DeletePanel({ portfolios, onDelete, onSaved }: DeletePanelProps) {
  const action = actionById("delete");
  const write = useWrite();

  const [portfolioId, setPortfolioId] = useState<number | null>(
    portfolios[0]?.portfolio_id ?? null,
  );
  const [typed, setTyped] = useState("");

  const chosen = useMemo(
    () => portfolios.find((row) => row.portfolio_id === portfolioId) ?? null,
    [portfolios, portfolioId],
  );
  const impact = useMemo(() => (chosen === null ? null : deleteImpact(chosen)), [chosen]);

  const confirmed = impact !== null && typed.trim() === impact.confirmPhrase;

  async function commit(): Promise<void> {
    if (impact === null || !confirmed) return;
    await write.run(
      action,
      onDelete === undefined ? undefined : () => onDelete(impact.portfolio.portfolio_id),
      () => {
        setTyped("");
        onSaved(successSentence(action, impact.portfolio.name));
      },
    );
  }

  if (portfolios.length === 0) {
    return (
      <div className="space-y-4" data-testid="delete-panel">
        <PanelHeading title={action.title}>{action.blurb}</PanelHeading>
        <Notice tone="warning">There is no portfolio to delete.</Notice>
      </div>
    );
  }

  return (
    <div className="space-y-4" data-testid="delete-panel">
      <PanelHeading title={action.title}>{action.blurb}</PanelHeading>

      <Field label="Portfolio to delete">
        {(id) => (
          <Select
            id={id}
            value={portfolioId === null ? "" : String(portfolioId)}
            onChange={(event) => {
              setPortfolioId(event.target.value === "" ? null : Number(event.target.value));
              setTyped("");
              write.clearFailure();
            }}
          >
            {portfolios.map((row) => (
              <option key={row.portfolio_id} value={String(row.portfolio_id)}>
                {row.name}
                {row.kind === "MONITORING" ? " (monitoring view)" : ""}
              </option>
            ))}
          </Select>
        )}
      </Field>

      {impact === null ? null : (
        <section
          aria-label="What deleting this does"
          data-testid="delete-impact"
          className="space-y-3 rounded-xl border border-border bg-card p-3"
        >
          <h4 className="text-sm font-semibold">What deleting {impact.portfolio.name} does</h4>

          <div className="flex flex-wrap gap-6">
            <MetricLine metric={impact.value} emphasis />
            <MetricLine metric={impact.cash} />
            <div>
              <p className="text-[0.6875rem] font-medium uppercase tracking-wide text-muted-foreground">
                Holdings in it
              </p>
              <p className="mt-0.5 text-sm font-semibold tabular-nums">{impact.holdingsCount}</p>
            </div>
          </div>

          {/* The reassurance first, and it is the one fact a person must not get wrong. */}
          <Notice testId="delete-holdings-fate">{impact.holdingsFate}</Notice>

          <div>
            <p className="flex items-center gap-1.5 text-xs font-medium">
              <CircleAlert aria-hidden="true" className="size-3.5 shrink-0 text-negative" />
              {/* The severity is a word as well as a colour and an icon. */}
              Permanently lost, and not rebuildable
            </p>
            <ul className="mt-1 space-y-1.5" data-testid="delete-losses">
              {impact.lost.map((loss) => (
                <li key={loss.what} className="text-xs leading-relaxed text-muted-foreground">
                  <span className="font-medium text-foreground">{loss.what}. </span>
                  {loss.detail}
                </li>
              ))}
            </ul>
          </div>

          <p className="text-xs leading-relaxed text-muted-foreground" data-testid="delete-children">
            {impact.childrenNote}
          </p>
        </section>
      )}

      {impact === null ? null : (
        <Field
          label={`Type “${impact.confirmPhrase}” to confirm`}
          hint="The list above has every portfolio in it. Typing the name is what makes sure this deletes the one you meant."
        >
          {(id) => (
            <Input
              id={id}
              value={typed}
              autoComplete="off"
              onChange={(event) => {
                setTyped(event.target.value);
                write.clearFailure();
              }}
            />
          )}
        </Field>
      )}

      <WriteFailure failure={write.failure} />

      <div className="flex flex-wrap items-center gap-2 border-t border-border pt-3">
        <Button
          type="button"
          variant="destructive"
          size="sm"
          disabled={!confirmed || write.saving || onDelete === undefined}
          onClick={() => void commit()}
        >
          <Trash2 aria-hidden="true" />
          {write.saving ? "Deleting…" : `Delete ${impact?.portfolio.name ?? "portfolio"}`}
        </Button>
        {confirmed ? (
          onDelete === undefined ? (
            <span className="text-xs text-muted-foreground" data-testid="delete-not-wired">
              This page has not passed a delete handler yet, so nothing would be removed.
            </span>
          ) : null
        ) : (
          <span className="text-xs text-muted-foreground" data-testid="delete-blocked-reason">
            Type the portfolio&rsquo;s name exactly to enable this.
          </span>
        )}
      </div>
    </div>
  );
}
