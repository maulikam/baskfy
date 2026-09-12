"use client";

import type { ScreenOut, ScreenRunResponse } from "@baskfy/api-client";
import type { Route } from "next";
import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";

import { BasketDetail } from "@/components/basket/basket-detail";
import { BasketHealthPanel } from "@/components/basket/basket-health";
import { SizingControls } from "@/components/basket/sizing-controls";
import { WeightMethodControls } from "@/components/basket/weight-method-controls";
import { EmptyState } from "@/components/data/empty-state";
import { ErrorState } from "@/components/data/error-state";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { assessBasketHealth, healthRowsFrom } from "@/lib/basket/health";
import { withBasketFactColumns } from "@/lib/basket/holding-facts";
import { materializeBasket, rowsFromScreen } from "@/lib/basket/materialize";
import {
  customWeightsForSave,
  DEFAULT_METHOD,
  fillMissingCustomWeights,
  methodPreviewNote,
  type WeightMethod,
} from "@/lib/basket/methods";
import { DEFAULT_PROFILE, resolveHoldings, resolveCashPct, suggestedCashPct, type HoldingProfile } from "@/lib/basket/profiles";
import { CreateBasketError, createBasketFromScreen } from "@/lib/create/fetch";
import { groupScreens } from "@/lib/create/screens";
import { defaultDefinition } from "@/lib/screens/defaults";
import { usePreview } from "@/lib/screens/queries";
import { parseDefinition } from "@/lib/screens/url-state";

type SaveState =
  | { status: "idle" }
  | { status: "saving" }
  | { status: "saved"; slug: string; name: string }
  | { status: "failed"; message: string };

/**
 * SB2 — turn a screen (template or the investor's own) into a sized private basket.
 *
 * The dropdown is the whole of "first login already has screens": `GET /screens` returns the
 * example templates for a new account, and this control lists them. Amount and name count live
 * in {@link SizingControls}; the profile suggests the count and an explicit number beats it.
 *
 * The numbers on screen are a preview. Save re-runs the named screen server-side
 * (`POST /cb/baskets/from-screen`) and stores that answer, which is why the confirmation links
 * to the saved basket rather than claiming the preview was stored.
 */
export function CreateFromScreen({
  screens,
  initialScreenId,
}: {
  screens: ScreenOut[];
  initialScreenId: string | null;
}) {
  const router = useRouter();
  const [selectedId, setSelectedId] = useState(initialScreenId);
  const selected = screens.find((screen) => screen.public_id === selectedId) ?? null;
  const definition = useMemo(
    () => (selected ? parseDefinition(selected.definition) : defaultDefinition()),
    [selected],
  );

  const preview = usePreview({
    definition,
    columns: withBasketFactColumns(selected?.columns ?? []),
    enabled: selected !== null,
  });

  if (screens.length === 0) {
    return (
      <EmptyState
        title="No screens to size yet"
        reason="Templates appear here on first login. If this is empty, the seed has not run — open Build and come back."
        action={{ label: "Open Build", onClick: () => router.push("/build") }}
      />
    );
  }

  return (
    <div className="flex w-full min-w-0 flex-col gap-6">
      <ScreenPicker
        screens={screens}
        selectedId={selectedId}
        onSelect={setSelectedId}
      />
      {selected ? (
        <SizedPreview
          key={selected.public_id}
          screen={selected}
          result={preview.data}
          isPending={preview.isPending}
          error={preview.error}
          onRetry={() => void preview.refetch()}
        />
      ) : null}
    </div>
  );
}

function ScreenPicker({
  screens,
  selectedId,
  onSelect,
}: {
  screens: ScreenOut[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  const { examples, mine } = groupScreens(screens);

  return (
    <div className="flex flex-col gap-2">
      <Label htmlFor="create-screen">Screen</Label>
      <Select
        id="create-screen"
        data-testid="create-screen-select"
        value={selectedId ?? ""}
        onChange={(event) => onSelect(event.target.value)}
      >
        {examples.length > 0 ? (
          <optgroup label="Templates — ready on first login">
            {examples.map((screen) => (
              <option key={screen.public_id} value={screen.public_id}>
                {screen.name}
              </option>
            ))}
          </optgroup>
        ) : null}
        {mine.length > 0 ? (
          <optgroup label="Your screens">
            {mine.map((screen) => (
              <option key={screen.public_id} value={screen.public_id}>
                {screen.name}
              </option>
            ))}
          </optgroup>
        ) : null}
      </Select>
      <p className="text-xs text-muted-foreground">
        {selectedId && examples.some((screen) => screen.public_id === selectedId)
          ? "A template. You can size it as-is; editing the rule itself needs a duplicate in Build."
          : "Any screen you have saved, or a template that shipped with the account."}
      </p>
    </div>
  );
}

function SizedPreview({
  screen,
  result,
  isPending,
  error,
  onRetry,
}: {
  screen: ScreenOut;
  result: ScreenRunResponse | undefined;
  isPending: boolean;
  error: unknown;
  onRetry: () => void;
}) {
  const router = useRouter();
  const [name, setName] = useState(screen.name);
  const [amount, setAmount] = useState<number | null>(null);
  const [profile, setProfile] = useState<HoldingProfile>(DEFAULT_PROFILE);
  const [holdings, setHoldings] = useState<number | null>(null);
  const [cashPct, setCashPct] = useState<number | null>(null);
  const [method, setMethod] = useState<WeightMethod>(DEFAULT_METHOD);
  const [customWeights, setCustomWeights] = useState<Record<string, number>>({});
  const [save, setSave] = useState<SaveState>({ status: "idle" });

  const rows = useMemo(
    () => rowsFromScreen((result?.rows ?? []) as Record<string, unknown>[]),
    [result],
  );

  const basket = useMemo(
    () =>
      materializeBasket({
        name: name.trim() || screen.name,
        asOf: result?.as_of,
        rows,
        holdings,
        profile,
        notional: amount ?? 0,
        cashPct,
        method,
        customWeights:
          method === "CUSTOM"
            ? fillMissingCustomWeights(
                rows
                  .slice()
                  .sort((a, b) => a.rank - b.rank)
                  .slice(
                    0,
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
    [name, screen.name, result?.as_of, rows, holdings, profile, amount, cashPct, method, customWeights],
  );

  const health = useMemo(
    () =>
      assessBasketHealth(
        healthRowsFrom(basket.holdings, (result?.rows ?? []) as Record<string, unknown>[]),
      ),
    [basket.holdings, result?.rows],
  );

  async function onSave() {
    if (amount === null || amount <= 0) return;
    setSave({ status: "saving" });
    try {
      const custom = customWeightsForSave(
        method,
        basket.holdings.map((row) => row.symbol),
        customWeights,
      );
      const saved = await createBasketFromScreen({
        screen_public_id: screen.public_id,
        amount,
        name: name.trim() || screen.name,
        profile: holdings === null ? profile : null,
        holdings,
        cash_pct: resolveCashPct({ requested: cashPct }),
        method,
        ...(custom ? { custom_weights: custom } : {}),
      });
      setSave({ status: "saved", slug: saved.slug, name: saved.name });
    } catch (err) {
      setSave({
        status: "failed",
        message:
          err instanceof CreateBasketError ? err.message : "Could not save this basket.",
      });
    }
  }

  if (error && !result) {
    return <ErrorState error={error} onRetry={onRetry} />;
  }

  if (isPending && !result) {
    return (
      <p className="text-sm text-muted-foreground" data-testid="create-screen-loading">
        Running the screen…
      </p>
    );
  }

  if (result && result.result_count === 0) {
    return (
      <EmptyState
        title="This screen returned nothing"
        reason="Loosen the filters in Build and come back — a basket needs at least two names."
        action={{
          label: "Open this screen",
          onClick: () => router.push(`/build/${screen.public_id}`),
        }}
      />
    );
  }

  return (
    <div className="flex w-full min-w-0 flex-col gap-4">
      <div className="flex flex-col gap-2">
        <Label htmlFor="create-basket-name">Basket name</Label>
        <Input
          id="create-basket-name"
          value={name}
          onChange={(event) => {
            setName(event.target.value);
            setSave({ status: "idle" });
          }}
          autoComplete="off"
        />
      </div>

      <SizingControls
        amount={amount}
        onAmountChange={(value) => {
          setAmount(value);
          setSave({ status: "idle" });
        }}
        profile={profile}
        onProfileChange={(value) => {
          setProfile(value);
          setSave({ status: "idle" });
        }}
        holdings={holdings}
        onHoldingsChange={(value) => {
          setHoldings(value);
          setSave({ status: "idle" });
        }}
        cashPct={cashPct}
        onCashPctChange={(value) => {
          setCashPct(value);
          setSave({ status: "idle" });
        }}
        suggestedCashPct={suggestedCashPct(null)}
        available={rows.length}
        minimum={basket.minInvestment}
      />

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

      {result ? (
        <BasketDetail
          basket={basket}
          amount={amount !== null && amount > 0 ? amount : undefined}
        />
      ) : null}
      {result ? (
        <BasketHealthPanel
          health={health}
          backtestHref={`/build/backtests?screen=${screen.public_id}` as Route}
        />
      ) : null}

      <div className="flex flex-wrap items-center gap-3">
        <Button
          type="button"
          data-testid="save-from-screen"
          onClick={() => void onSave()}
          disabled={
            save.status === "saving" ||
            basket.holdings.length < 2 ||
            amount === null ||
            amount <= 0 ||
            amount < basket.minInvestment
          }
        >
          {save.status === "saving" ? "Saving…" : "Save as basket"}
        </Button>
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
      <p className="text-xs text-muted-foreground">
        Saving records the basket. Nothing here buys or sells anything.
      </p>
    </div>
  );
}
