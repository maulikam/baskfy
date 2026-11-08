"use client";

import { useState, useTransition } from "react";

import { reprocessInstrument, type AdminActionResult } from "@/app/actions/admin";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";

/**
 * PROMPTS.md Prompt 17 §4's "reprocess-instrument action" — docs/09 §"Adjustment algorithm"'s
 * rebuild rule, as a button.
 *
 * Safe to press when you are only fairly sure: the rebuild is idempotent, so a healthy history is
 * rewritten to identical values. What it does **not** do is recompute factors — the windows that
 * include the corrected bars need re-running afterwards, which is why the copy says so rather than
 * letting an operator assume otherwise.
 */
export function ReprocessForm() {
  const [symbol, setSymbol] = useState("");
  const [result, setResult] = useState<AdminActionResult | null>(null);
  const [pending, startTransition] = useTransition();

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end gap-3">
        <div className="w-56">
          <Label htmlFor="reprocess-symbol">NSE symbol</Label>
          <Input
            id="reprocess-symbol"
            value={symbol}
            onChange={(event) => setSymbol(event.target.value)}
            placeholder="CUPID"
            autoComplete="off"
          />
        </div>
        <Button
          disabled={pending}
          onClick={() =>
            startTransition(async () => {
              setResult(await reprocessInstrument(symbol));
            })
          }
        >
          Rebuild adjusted history
        </Button>
      </div>
      <p className="max-w-prose text-xs text-muted-foreground">
        Rebuilds <code className="font-mono">close/open/high/low/volume</code> from{" "}
        <code className="font-mono">close_raw</code> and the corporate-action history. Idempotent.
        Factors are <strong>not</strong> recomputed — re-run the nights whose windows include the
        corrected bars. Rights issues are never adjusted (TERP needs a subscription price NSE
        usually omits); the step payload reports them as unquantified.
      </p>
      {result ? (
        <p
          role="status"
          className={cn(
            "rounded border px-3 py-2 text-sm",
            result.ok
              ? "border-positive/30 bg-positive/10 text-positive"
              : "border-negative/30 bg-negative/10 text-negative",
          )}
        >
          {result.message}
        </p>
      ) : null}
    </div>
  );
}
