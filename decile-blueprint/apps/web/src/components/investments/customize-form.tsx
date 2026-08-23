"use client";

import { useState } from "react";

import { PlanHandoffPanel } from "@/components/cb/plan-handoff-panel";
import { Button } from "@/components/ui/button";
import { apiOrigin } from "@/lib/api/config";

/**
 * Customize constituents — posts weight diffs to the CUSTOMIZE plan preview.
 * Never calls execute / OrderGateway; success opens PlanHandoffPanel.
 */

export interface CustomizeFormProps {
  investmentId: string;
  basketName: string;
}

interface PlanPreview {
  market_open?: boolean;
  kind?: string;
  desk_plan_id?: string;
  expires_at_hint?: string;
  next_open_ist?: string;
}

export function CustomizeForm({ investmentId, basketName }: CustomizeFormProps) {
  const [weightsJson, setWeightsJson] = useState(
    '{\n  "AAA": "0.5",\n  "BBB": "0.5"\n}',
  );
  const [holdingsJson, setHoldingsJson] = useState('{\n  "AAA": 10,\n  "BBB": 0\n}');
  const [pricesJson, setPricesJson] = useState(
    '{\n  "AAA": "100",\n  "BBB": "100"\n}',
  );
  const [amount, setAmount] = useState("1000");
  const [error, setError] = useState<string | null>(null);
  const [plan, setPlan] = useState<PlanPreview | null>(null);
  const [pending, setPending] = useState(false);

  async function onPreview() {
    setError(null);
    setPlan(null);
    setPending(true);
    try {
      const target_weights = JSON.parse(weightsJson) as Record<string, string>;
      const holdings = JSON.parse(holdingsJson) as Record<string, number>;
      const prices = JSON.parse(pricesJson) as Record<string, string>;
      const response = await fetch(
        `${apiOrigin()}/api/v1/cb/investments/${encodeURIComponent(investmentId)}/customize`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            holdings,
            target_weights,
            prices,
            amount,
          }),
        },
      );
      const body = (await response.json()) as PlanPreview;
      if (!response.ok) {
        setError("Customize preview failed — check weights and try again.");
        return;
      }
      if (body.market_open === false) {
        setError(`Market closed. Next open ${body.next_open_ist ?? "—"}.`);
        return;
      }
      if (body.kind !== "CUSTOMIZE" || !body.desk_plan_id) {
        setError("Unexpected preview response.");
        return;
      }
      setPlan(body);
    } catch {
      setError("Could not parse inputs or reach the plan API.");
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="space-y-4">
      <label className="block space-y-1 text-sm">
        <span className="font-medium">Current holdings (JSON)</span>
        <textarea
          className="min-h-24 w-full rounded-md border border-border bg-background p-2 font-mono text-xs"
          value={holdingsJson}
          onChange={(e) => setHoldingsJson(e.target.value)}
          spellCheck={false}
        />
      </label>
      <label className="block space-y-1 text-sm">
        <span className="font-medium">Target weights (JSON)</span>
        <textarea
          className="min-h-24 w-full rounded-md border border-border bg-background p-2 font-mono text-xs"
          value={weightsJson}
          onChange={(e) => setWeightsJson(e.target.value)}
          spellCheck={false}
        />
      </label>
      <label className="block space-y-1 text-sm">
        <span className="font-medium">Prices (JSON)</span>
        <textarea
          className="min-h-24 w-full rounded-md border border-border bg-background p-2 font-mono text-xs"
          value={pricesJson}
          onChange={(e) => setPricesJson(e.target.value)}
          spellCheck={false}
        />
      </label>
      <label className="block space-y-1 text-sm">
        <span className="font-medium">Book amount (₹)</span>
        <input
          className="w-full rounded-md border border-border bg-background px-2 py-1.5 tabular-nums"
          value={amount}
          onChange={(e) => setAmount(e.target.value)}
        />
      </label>
      <Button type="button" variant="primary" size="sm" disabled={pending} onClick={onPreview}>
        {pending ? "Building preview…" : "Preview customize plan"}
      </Button>
      {error ? <p className="text-sm text-destructive">{error}</p> : null}
      {plan?.desk_plan_id ? (
        <PlanHandoffPanel
          basketName={`${basketName} · customize`}
          planId={plan.desk_plan_id}
          expiresAt={plan.expires_at_hint ?? null}
        />
      ) : null}
      <p className="text-xs text-muted-foreground">
        This page only builds a CUSTOMIZE plan preview. Execution stays on the desk — there is no
        execute control here.
      </p>
    </div>
  );
}
