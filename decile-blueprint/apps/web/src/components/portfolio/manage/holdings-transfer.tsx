"use client";

import { useMemo, useState } from "react";

import { HoldingsPicker } from "@/components/portfolio/holdings-picker";
import { Field, Notice, PanelHeading } from "@/components/portfolio/manage/panel-chrome";
import { TransferPreview } from "@/components/portfolio/manage/transfer-preview";
import { WriteFailure, useWrite } from "@/components/portfolio/manage/write-state";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/select";
import {
  actionById,
  previewAssignment,
  successSentence,
  targetFromRow,
  toTransferRequest,
  type AssignmentTarget,
  type ManageOutcome,
  type TransferRequest,
} from "@/lib/portfolio/manage";
import type { AggregatedHolding } from "@/lib/portfolio/organize";
import type { PortfolioRow } from "@/lib/portfolio/overview";

/**
 * Assign holdings, and move them between capital portfolios — one panel, two intents.
 *
 * They are one component because they are one operation with a different starting place, and the
 * API agrees: both are `POST /portfolio/{id}/holdings`, which *moves* shares — free ones first,
 * then the smallest other slice, so Unallocated drains before a portfolio the user built is
 * disturbed. Splitting them into two forms would mean two places to get the exclusivity wording
 * right and two places to forget the source side.
 *
 * What differs is what the reader must be shown:
 *
 *   · **Assign** is Unallocated → a capital portfolio. The thing that can go wrong is
 *     exclusivity, so every refused line names the portfolio that already holds the share.
 *   · **Move** is capital → capital. Nothing can go wrong arithmetically — the server will do it
 *     — so the thing that must not go wrong is the user's *understanding*: the portfolio they
 *     were not looking at is about to get smaller, and {@link TransferPreview} shows it at the
 *     same size as the one they were.
 *
 * The quantity boxes are the shipped picker's, deliberately. Its ceiling is the route's ceiling
 * (the whole position, less what the destination already holds) and its arithmetic is the
 * `bigint` decimal path the rest of the product uses. Re-implementing it here would be a second
 * answer to "how many shares may this leg contribute", and the first one is already tested.
 */

export interface HoldingsTransferProps {
  /** `ASSIGN` files from Unallocated; `MOVE` reallocates between capital portfolios. */
  intent: "ASSIGN" | "MOVE";
  rows: readonly AggregatedHolding[];
  sectors?: Readonly<Record<string, string>> | undefined;
  /** Capital portfolios only. A monitoring view owns nothing and cannot be either side. */
  capital: readonly PortfolioRow[];
  onTransfer?: ((request: TransferRequest) => Promise<ManageOutcome>) | undefined;
  /** Called only after a write that genuinely succeeded. */
  onSaved: (message: string) => void;
}

export function HoldingsTransfer({
  intent,
  rows,
  sectors,
  capital,
  onTransfer,
  onSaved,
}: HoldingsTransferProps) {
  const action = actionById(intent === "MOVE" ? "move" : "assign");
  const write = useWrite();

  const targets = useMemo<readonly AssignmentTarget[]>(
    () => capital.filter((row) => row.kind === "CAPITAL").map(targetFromRow),
    [capital],
  );

  const [sourceId, setSourceId] = useState<number | null>(
    intent === "MOVE" ? (targets[0]?.portfolio_id ?? null) : null,
  );
  const [destinationId, setDestinationId] = useState<number | null>(
    intent === "MOVE" ? (targets[1]?.portfolio_id ?? null) : (targets[0]?.portfolio_id ?? null),
  );
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [quantities, setQuantities] = useState<ReadonlyMap<string, string>>(new Map());

  const source = targets.find((target) => target.portfolio_id === sourceId) ?? null;
  const destination = targets.find((target) => target.portfolio_id === destinationId) ?? null;

  /**
   * For a MOVE, only the legs the source actually holds are worth offering. Showing the whole
   * book and refusing four-fifths of it is a list that teaches the user nothing; narrowing the
   * list means the refusals that remain are about quantity, which is a thing they can fix.
   */
  const pickable = useMemo(() => {
    if (intent !== "MOVE" || source === null) return rows;
    return rows.filter((row) =>
      (row.brokers ?? []).some((line) =>
        (line.allocations ?? []).some(
          (slice) => slice.portfolio.portfolio_id === source.portfolio_id,
        ),
      ),
    );
  }, [intent, rows, source]);

  const preview = useMemo(
    () =>
      previewAssignment({
        intent,
        rows,
        selected,
        quantities,
        source,
        destination,
      }),
    [intent, rows, selected, quantities, source, destination],
  );

  const request = toTransferRequest(preview);

  async function commit(): Promise<void> {
    if (request === null || destination === null) return;
    await write.run(
      action,
      onTransfer === undefined ? undefined : () => onTransfer(request),
      () => {
        setSelected(new Set());
        setQuantities(new Map());
        onSaved(successSentence(action, destination.name));
      },
    );
  }

  const notWired = onTransfer === undefined;

  return (
    <div className="space-y-4" data-testid={`transfer-${intent.toLowerCase()}`}>
      <PanelHeading title={action.title}>{action.blurb}</PanelHeading>

      {targets.length === 0 ? (
        <Notice tone="warning" testId="transfer-no-targets">
          There is no capital portfolio to {intent === "MOVE" ? "move between" : "file into"} yet.
          Create one first — <em>Create a portfolio</em>, above.
        </Notice>
      ) : (
        <>
          <div className="grid gap-3 sm:grid-cols-2">
            {intent === "MOVE" ? (
              <Field
                label="Out of"
                hint="Only the holdings this portfolio actually holds are offered below."
              >
                {(id) => (
                  <Select
                    id={id}
                    value={sourceId === null ? "" : String(sourceId)}
                    onChange={(event) => {
                      setSourceId(event.target.value === "" ? null : Number(event.target.value));
                      setSelected(new Set());
                      write.clearFailure();
                    }}
                  >
                    <option value="">Choose a portfolio</option>
                    {targets.map((target) => (
                      <option key={target.portfolio_id} value={String(target.portfolio_id)}>
                        {target.name}
                      </option>
                    ))}
                  </Select>
                )}
              </Field>
            ) : (
              /* A fact, not a control. A disabled select with one option is still something a
                 person tries to click; assigning always comes out of Unallocated, so it is
                 stated rather than offered. */
              <div className="space-y-1.5" data-testid="assign-source">
                <p className="text-[0.6875rem] font-medium uppercase tracking-wide text-muted-foreground">
                  Out of
                </p>
                <p className="pt-0.5 text-sm font-medium">Unallocated</p>
                <p className="text-xs leading-snug text-muted-foreground">
                  Assigning files shares that are in no portfolio yet. Use Move holdings to take
                  them out of one.
                </p>
              </div>
            )}

            <Field label="Into" hint="Capital portfolios only — a monitoring view owns nothing.">
              {(id) => (
                <Select
                  id={id}
                  value={destinationId === null ? "" : String(destinationId)}
                  onChange={(event) => {
                    setDestinationId(
                      event.target.value === "" ? null : Number(event.target.value),
                    );
                    write.clearFailure();
                  }}
                >
                  <option value="">Choose a portfolio</option>
                  {targets.map((target) => (
                    <option key={target.portfolio_id} value={String(target.portfolio_id)}>
                      {target.name}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
          </div>

          {intent === "MOVE" ? (
            /* The two ceilings differ and a reader deserves to be told which is which. The
               picker's "of N available" is the ROUTE's ceiling — `POST /portfolio/{id}/holdings`
               takes free shares first and then the smallest other slice, so it would happily take
               45 of a holding the source portfolio only has 30 of. A *move* out of a named
               portfolio cannot mean that, so the preview below refuses it and says who holds the
               rest. Without this sentence the two numbers look like a bug. */
            <Notice testId="move-ceiling-notice">
              The count beside each holding is everything that could be taken from anywhere. A move
              takes only what {source?.name ?? "the source portfolio"} itself holds — ask for more
              and the preview below will say who holds the rest.
            </Notice>
          ) : null}

          <HoldingsPicker
            rows={pickable}
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
            targetPortfolioId={intent === "MOVE" ? destinationId : null}
            portfolioName={destination?.name ?? "Choose a portfolio"}
          />

          <TransferPreview preview={preview} />

          <WriteFailure failure={write.failure} />

          <div className="flex flex-wrap items-center gap-2 border-t border-border pt-3">
            <Button
              type="button"
              variant="primary"
              size="sm"
              disabled={request === null || write.saving || notWired}
              onClick={() => void commit()}
            >
              {write.saving
                ? "Saving…"
                : intent === "MOVE"
                  ? `Move into ${destination?.name ?? "portfolio"}`
                  : `Assign to ${destination?.name ?? "portfolio"}`}
            </Button>
            {/* A disabled primary action always says why, beside itself. PC1 removed this exact
                defect from this screen once already. */}
            {preview.blockedReason === null ? null : (
              <span className="text-xs text-muted-foreground" data-testid="transfer-blocked-reason">
                {preview.blockedReason}
              </span>
            )}
            {notWired && preview.blockedReason === null ? (
              <span className="text-xs text-muted-foreground" data-testid="transfer-not-wired">
                This page has not passed a save handler yet, so nothing would be written. Your
                selection stays on screen.
              </span>
            ) : null}
          </div>
        </>
      )}
    </div>
  );
}
