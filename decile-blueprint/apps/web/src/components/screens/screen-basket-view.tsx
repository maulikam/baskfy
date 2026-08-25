"use client";

import type { ScreenRunResponse } from "@baskfy/api-client";
import { useMemo, useState } from "react";

import { BasketDetail } from "@/components/basket/basket-detail";
import { BasketHealthPanel } from "@/components/basket/basket-health";
import { SizingControls } from "@/components/basket/sizing-controls";
import { WeightMethodControls } from "@/components/basket/weight-method-controls";
import { assessBasketHealth, healthRowsFrom } from "@/lib/basket/health";
import { materializeBasket, DEFAULT_NOTIONAL, rowsFromScreen } from "@/lib/basket/materialize";
import {
  customWeightsForSave,
  DEFAULT_METHOD,
  fillMissingCustomWeights,
  methodPreviewNote,
  type WeightMethod,
} from "@/lib/basket/methods";
import { DEFAULT_PROFILE, resolveHoldings, resolveCashPct, suggestedCashPct, type HoldingProfile } from "@/lib/basket/profiles";
import { CreateBasketError, createBasketFromScreen } from "@/lib/create/fetch";
import { screenDefaultView, type ScreenDefaultView } from "@/lib/screens/feature-flags";
import { cn } from "@/lib/utils";

type SaveState =
  | { status: "idle" }
  | { status: "saving" }
  | { status: "saved"; slug: string; name: string }
  | { status: "failed"; message: string };

/**
 * Which view is on screen before anything is clicked.
 *
 * Tree 7 D8 moves the *editor's* default, and the editor is the caller that leaves `topN`
 * undefined. A compact card preview pins its own name count and is a basket by construction, so it
 * stays basket-first whatever the flag says — flipping the flag cannot change what a card shows.
 */
function initialMode(topN: number | undefined): ScreenDefaultView {
  return topN === undefined ? screenDefaultView() : "basket";
}

/**
 * The two views of a screen run — the ranked table and the investable basket — behind one toggle.
 *
 * Which one a first paint shows is `screenDefaultView()`, and it is `table` (Tree 7 D8): on
 * `/build/[id]` the ranked list is the artifact being built, and on a read-only example screen the
 * sizing form is a dead end because nothing can be saved. Basket stays one tap away, unchanged.
 * Tree 6 §5's basket-first default is what `NEXT_PUBLIC_SCREEN_DEFAULT_VIEW=basket` restores, and
 * it is still the default on every surface Tree 6 owns — none of them renders this component.
 *
 * SB1 adds the sizing the screen itself cannot answer — amount, holding profile, name count — and
 * a save that turns the result into a `source = 'SCREEN'` basket. The numbers on screen are a
 * preview: the save endpoint re-runs the screen and its answer is the record, which is why the
 * confirmation links to the saved basket rather than claiming the preview was stored.
 */
export function ScreenBasketView({
  result,
  screenName,
  screenPublicId,
  topN,
  exposureTier,
  table,
}: {
  result: ScreenRunResponse;
  screenName: string;
  /** Omitted for an unsaved preview, where there is no screen to attribute a basket to. */
  screenPublicId?: string | undefined;
  /** A fixed count that skips the profile entirely — used by the compact card previews. */
  topN?: number | undefined;
  /** The desk's current exposure tier (R1–R4), which decides the cash share. */
  exposureTier?: string | null | undefined;
  table: React.ReactNode;
}) {
  /* Lazy: the flag decides the first paint only, so a re-render must never reset a chosen view. */
  const [mode, setMode] = useState<ScreenDefaultView>(() => initialMode(topN));
  const [amount, setAmount] = useState<number | null>(() => (topN === undefined ? null : DEFAULT_NOTIONAL));
  const [profile, setProfile] = useState<HoldingProfile>(DEFAULT_PROFILE);
  const [holdings, setHoldings] = useState<number | null>(null);
  const [cashPct, setCashPct] = useState<number | null>(null);
  const [method, setMethod] = useState<WeightMethod>(DEFAULT_METHOD);
  const [customWeights, setCustomWeights] = useState<Record<string, number>>({});
  const [save, setSave] = useState<SaveState>({ status: "idle" });

  const rows = useMemo(
    () => rowsFromScreen(result.rows as Record<string, unknown>[]),
    [result],
  );

  const basket = useMemo(
    () =>
      materializeBasket({
        name: screenName,
        asOf: result.as_of,
        rows,
        topN,
        holdings,
        profile,
        exposureTier,
        cashPct,
        notional: topN !== undefined ? DEFAULT_NOTIONAL : (amount ?? 0),
        method,
        customWeights:
          method === "CUSTOM"
            ? fillMissingCustomWeights(
                rows
                  .slice()
                  .sort((a, b) => a.rank - b.rank)
                  .slice(
                    0,
                    topN ??
                      resolveHoldings({
                        available: rows.length,
                        profile,
                        requested: holdings,
                      }),
                  )
                  .map((row) => row.symbol),
                customWeights,
              )
            : null,
        source: "preview",
      }),
    [screenName, result.as_of, rows, topN, holdings, profile, exposureTier, amount, cashPct, method, customWeights],
  );

  const health = useMemo(
    () =>
      assessBasketHealth(
        healthRowsFrom(basket.holdings, result.rows as Record<string, unknown>[]),
      ),
    [basket.holdings, result.rows],
  );

  const sizable = topN === undefined;

  async function onSave() {
    if (!screenPublicId || amount === null || amount <= 0) return;
    setSave({ status: "saving" });
    try {
      const custom = customWeightsForSave(
        method,
        basket.holdings.map((row) => row.symbol),
        customWeights,
      );
      const saved = await createBasketFromScreen({
        screen_public_id: screenPublicId,
        amount,
        name: screenName,
        profile: holdings === null ? profile : null,
        holdings,
        exposure_tier: exposureTier ?? null,
        cash_pct: resolveCashPct({ exposureTier, requested: cashPct }),
        method,
        ...(custom ? { custom_weights: custom } : {}),
      });
      setSave({ status: "saved", slug: saved.slug, name: saved.name });
    } catch (error) {
      setSave({
        status: "failed",
        message:
          error instanceof CreateBasketError ? error.message : "Could not save this basket.",
      });
    }
  }

  return (
    <div className="flex w-full min-w-0 flex-col gap-3">
      <div
        className="inline-flex w-fit gap-1 rounded-lg border border-border/70 bg-muted/40 p-1"
        role="group"
        aria-label="Result view"
      >
        {(
          [
            ["basket", "Basket"],
            ["table", "Table"],
          ] as const
        ).map(([id, label]) => (
          <button
            key={id}
            type="button"
            data-testid={`view-mode-${id}`}
            aria-pressed={mode === id}
            onClick={() => setMode(id)}
            className={cn(
              "rounded-md px-3 py-1.5 text-sm transition-colors",
              mode === id
                ? "marker-control font-medium"
                : "text-muted-foreground hover:bg-background hover:text-foreground",
            )}
          >
            {label}
          </button>
        ))}
      </div>

      {mode === "basket" ? (
        <>
          {sizable ? (
            <SizingControls
              amount={amount}
              onAmountChange={setAmount}
              profile={profile}
              onProfileChange={setProfile}
              holdings={holdings}
              onHoldingsChange={setHoldings}
              cashPct={cashPct}
              onCashPctChange={setCashPct}
              suggestedCashPct={suggestedCashPct(exposureTier)}
              available={rows.length}
              minimum={basket.minInvestment}
            />
          ) : null}

          {sizable ? (
            <WeightMethodControls
              method={method}
              onMethodChange={(value) => {
                setMethod(value);
                setSave({ status: "idle" });
              }}
              rows={basket.holdings.map((row) => ({
                symbol: row.symbol,
                name: row.name,
                facts: row.facts,
              }))}
              customWeights={customWeights}
              onCustomWeightsChange={(value) => {
                setCustomWeights(value);
                setSave({ status: "idle" });
              }}
              note={methodPreviewNote(
                method,
                basket.holdings.map((row) => {
                  const source = rows.find((candidate) => candidate.symbol === row.symbol);
                  return {
                    symbol: row.symbol,
                    rank: row.rank,
                    score: source?.sorting_factor,
                    vol: source?.vol,
                  };
                }),
              )}
            />
          ) : null}

          <BasketDetail
            basket={basket}
            amount={sizable && amount !== null && amount > 0 ? amount : undefined}
          />
          <BasketHealthPanel
            health={health}
            backtestHref={
              screenPublicId ? `/build/backtests?screen=${screenPublicId}` : undefined
            }
          />

          {sizable && screenPublicId ? (
            <div className="flex flex-wrap items-center gap-3">
              <button
                type="button"
                data-testid="save-basket"
                onClick={() => void onSave()}
                disabled={
                  save.status === "saving" ||
                  basket.holdings.length === 0 ||
                  amount === null ||
                  amount <= 0 ||
                  amount < basket.minInvestment
                }
                className="rounded-lg bg-accent px-4 py-2 text-sm font-medium text-accent-foreground transition-opacity hover:opacity-90 disabled:opacity-50"
              >
                {save.status === "saving" ? "Saving…" : "Save as basket"}
              </button>
              <p aria-live="polite" className="text-sm">
                {save.status === "saved" ? (
                  <a
                    href={`/basket/${save.slug}`}
                    className="text-accent underline-offset-4 hover:underline"
                  >
                    Saved “{save.name}” — open it
                  </a>
                ) : null}
                {save.status === "failed" ? (
                  <span className="text-warning-foreground">{save.message}</span>
                ) : null}
              </p>
            </div>
          ) : null}

          <p className="text-xs text-muted-foreground">
            Saving records the basket. Nothing here buys or sells anything.
          </p>
        </>
      ) : (
        table
      )}
    </div>
  );
}
