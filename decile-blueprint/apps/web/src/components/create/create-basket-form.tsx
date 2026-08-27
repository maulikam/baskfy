"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  CreateBasketError,
  createPrivateBasket,
} from "@/lib/create/fetch";
import {
  assertWeightsSumToOne,
  equalWeights,
  normalizeWeights,
} from "@/lib/create/weights";

/**
 * SC8 create form — private basket, ≥2 instruments, equal/custom weights normalize to 1.0.
 * Saves via `POST /api/v1/cb/discover` (leaf 3.4). Preview stubbed.
 * No order route — invest later via the SC3 plan path.
 */

const MIN_INSTRUMENTS = 2;

type WeightScheme = "equal" | "custom";

interface Row {
  symbol: string;
  weightInput: string;
}

export interface SavedPrivateBasket {
  id: number;
  slug: string;
  name: string;
  /** Always "PRIVATE" today; typed as string because the API may add values. */
  visibility: string;
  constituents: { symbol: string; weight: number }[];
}

function blankRows(n: number): Row[] {
  return Array.from({ length: n }, () => ({ symbol: "", weightInput: "" }));
}

function weightAsNumber(value: string | number): number {
  return typeof value === "number" ? value : Number.parseFloat(value);
}

export function CreateBasketForm() {
  const [name, setName] = useState("");
  const [scheme, setScheme] = useState<WeightScheme>("equal");
  const [rows, setRows] = useState<Row[]>(blankRows(MIN_INSTRUMENTS));
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState<SavedPrivateBasket | null>(null);
  const [previewNote] = useState(
    "Backtest preview is stubbed for this leaf — point-in-time preview wires in with the screener path.",
  );

  function updateRow(index: number, patch: Partial<Row>) {
    setRows((prev) => prev.map((row, i) => (i === index ? { ...row, ...patch } : row)));
    setSaved(null);
  }

  function addRow() {
    setRows((prev) => [...prev, { symbol: "", weightInput: "" }]);
    setSaved(null);
  }

  function removeRow(index: number) {
    setRows((prev) => (prev.length <= MIN_INSTRUMENTS ? prev : prev.filter((_, i) => i !== index)));
    setSaved(null);
  }

  function resolvedWeights(symbols: string[]): number[] {
    if (scheme === "equal") return equalWeights(symbols.length);
    const raw = rows
      .filter((r) => r.symbol.trim())
      .map((r) => Number.parseFloat(r.weightInput) || 0);
    return normalizeWeights(raw);
  }

  async function onSave(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    const trimmed = name.trim();
    if (!trimmed) {
      setError("Give your basket a name.");
      return;
    }
    const symbols = rows.map((r) => r.symbol.trim().toUpperCase()).filter(Boolean);
    if (symbols.length < MIN_INSTRUMENTS) {
      setError(`Add at least ${MIN_INSTRUMENTS} instruments.`);
      return;
    }
    if (new Set(symbols).size !== symbols.length) {
      setError("Each symbol can appear only once.");
      return;
    }

    let weights: number[];
    try {
      weights = resolvedWeights(symbols);
      assertWeightsSumToOne(weights);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not normalize weights.");
      return;
    }

    setSaving(true);
    try {
      const result = await createPrivateBasket({
        name: trimmed,
        constituents: symbols.map((symbol, i) => ({
          symbol,
          weight: weights[i]!,
        })),
      });
      setSaved({
        id: result.id,
        slug: result.slug,
        name: result.name,
        visibility: result.visibility,
        constituents: result.constituents.map((c) => ({
          symbol: c.symbol,
          weight: weightAsNumber(c.weight),
        })),
      });
    } catch (err) {
      if (err instanceof CreateBasketError) {
        setError(err.message);
      } else {
        setError(err instanceof Error ? err.message : "Could not save basket.");
      }
    } finally {
      setSaving(false);
    }
  }

  return (
    <form
      onSubmit={(event) => {
        // onSave is async; the handler must not return a Promise (no-misused-promises).
        void onSave(event);
      }} className="flex max-w-xl flex-col gap-6">
      <p className="rounded-md border border-border bg-muted/40 px-3 py-2 text-sm text-muted-foreground">
        This basket stays <span className="font-medium text-foreground">PRIVATE</span> — only you
        see it. It never appears in the public Explore catalog.
      </p>

      <div className="flex flex-col gap-2">
        <Label htmlFor="basket-name">Name</Label>
        <Input
          id="basket-name"
          value={name}
          onChange={(e) => {
            setName(e.target.value);
            setSaved(null);
          }}
          placeholder="My momentum basket"
          autoComplete="off"
        />
      </div>

      <fieldset className="flex flex-col gap-3">
        <legend className="text-sm font-medium">Weighting</legend>
        <div className="flex gap-4 text-sm">
          <label className="flex items-center gap-2">
            <input
              type="radio"
              name="scheme"
              checked={scheme === "equal"}
              onChange={() => {
                setScheme("equal");
                setSaved(null);
              }}
            />
            Equal
          </label>
          <label className="flex items-center gap-2">
            <input
              type="radio"
              name="scheme"
              checked={scheme === "custom"}
              onChange={() => {
                setScheme("custom");
                setSaved(null);
              }}
            />
            Custom (normalize to 1.0)
          </label>
        </div>
      </fieldset>

      <div className="flex flex-col gap-3">
        <div className="flex items-baseline justify-between gap-2">
          <Label>Instruments (min {MIN_INSTRUMENTS})</Label>
          <Button type="button" variant="outline" size="sm" onClick={addRow}>
            Add symbol
          </Button>
        </div>
        <ul className="flex flex-col gap-2">
          {rows.map((row, index) => (
            <li key={index} className="flex flex-wrap items-end gap-2">
              <div className="flex min-w-[8rem] flex-1 flex-col gap-1">
                <span className="text-xs text-muted-foreground">Symbol</span>
                <Input
                  value={row.symbol}
                  onChange={(e) => updateRow(index, { symbol: e.target.value })}
                  placeholder="RELIANCE"
                  autoComplete="off"
                  className="uppercase"
                />
              </div>
              {scheme === "custom" ? (
                <div className="flex w-28 flex-col gap-1">
                  <span className="text-xs text-muted-foreground">Weight</span>
                  <Input
                    type="number"
                    inputMode="decimal"
                    step="0.0001"
                    min="0"
                    value={row.weightInput}
                    onChange={(e) => updateRow(index, { weightInput: e.target.value })}
                    placeholder="0.5"
                  />
                </div>
              ) : null}
              <Button
                type="button"
                variant="ghost"
                size="sm"
                disabled={rows.length <= MIN_INSTRUMENTS}
                onClick={() => removeRow(index)}
              >
                Remove
              </Button>
            </li>
          ))}
        </ul>
      </div>

      <div className="rounded-md border border-dashed border-border px-3 py-3 text-sm text-muted-foreground">
        <p className="font-medium text-foreground">Preview</p>
        <p className="mt-1">{previewNote}</p>
      </div>

      {error ? (
        <p className="text-sm text-destructive" role="alert">
          {error}
        </p>
      ) : null}

      <Button type="submit" disabled={saving}>
        {saving ? "Saving…" : "Save as private basket"}
      </Button>

      {saved ? (
        <div
          className="rounded-md border border-border bg-muted/30 px-3 py-3 text-sm"
          data-visibility={saved.visibility}
          data-basket-id={saved.id}
        >
          <p className="font-medium">
            Saved · {saved.name}{" "}
            <span className="text-muted-foreground">({saved.visibility})</span>
          </p>
          <p className="mt-1 font-mono text-xs text-muted-foreground">
            id {saved.id} · {saved.slug}
          </p>
          <ul className="mt-2 space-y-1 font-mono text-xs">
            {saved.constituents.map((c) => (
              <li key={c.symbol}>
                {c.symbol} · {(c.weight * 100).toFixed(2)}%
              </li>
            ))}
          </ul>
          <p className="mt-2 text-muted-foreground">
            Weights sum to 1.0. Invest later via the order-plan hand-off — this page never
            places an order.
          </p>
        </div>
      ) : null}
    </form>
  );
}
