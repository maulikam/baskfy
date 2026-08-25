"use client";

import { useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import {
  DriftError,
  type DriftScanResult,
  fixDrift,
  parseBrokerLines,
  scanDrift,
} from "@/lib/investments/drift";

/**
 * T8.6 — compare intended qty to broker qty and rebase the ledger. No broker instructions.
 */

export function DriftRepair({ investmentId }: { investmentId: string }) {
  const [holdingsText, setHoldingsText] = useState("");
  const [scan, setScan] = useState<DriftScanResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fixed, setFixed] = useState<string | null>(null);

  function lots() {
    return parseBrokerLines(holdingsText);
  }

  async function onScan(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setFixed(null);
    try {
      const brokerHoldings = lots();
      if (brokerHoldings.length < 1) {
        setError("List broker qty: SYMBOL QTY, one per line.");
        return;
      }
      setBusy(true);
      setScan(await scanDrift(investmentId, brokerHoldings));
    } catch (caught) {
      setError(caught instanceof DriftError ? caught.message : "Could not scan holdings.");
    } finally {
      setBusy(false);
    }
  }

  async function onFix() {
    setError(null);
    try {
      const brokerHoldings = lots();
      setBusy(true);
      const result = await fixDrift(investmentId, brokerHoldings);
      setFixed(
        `Ledger rebased to ${result.holdings.length} holding${result.holdings.length === 1 ? "" : "s"}.`,
      );
      setScan(null);
    } catch (caught) {
      setError(caught instanceof DriftError ? caught.message : "Could not rebase the ledger.");
    } finally {
      setBusy(false);
    }
  }

  const needsFix = scan?.action_type === "DRIFT" || (scan != null && scan.deltas.length > 0);

  return (
    <form
      data-testid="drift-repair"
      onSubmit={onScan}
      className="flex flex-col gap-3 rounded-xl border border-border bg-card p-4"
    >
      <div className="space-y-1">
        <h3 className="text-sm font-semibold text-foreground">Repair drift</h3>
        <p className="text-sm leading-relaxed text-muted-foreground">
          Paste what the broker shows. Fix now updates Baskfy&apos;s book to match. It does not
          send instructions to the broker.
        </p>
      </div>
      <div className="space-y-1">
        <Label htmlFor="drift-holdings">Broker holdings — one per line: SYMBOL QTY</Label>
        <textarea
          id="drift-holdings"
          value={holdingsText}
          onChange={(event) => setHoldingsText(event.target.value)}
          placeholder={"CUPID 4"}
          rows={3}
          className="w-full rounded-md border border-input bg-card px-3 py-2 text-sm"
        />
      </div>
      {scan && scan.deltas.length > 0 ? (
        <ul className="space-y-1 text-sm">
          {scan.deltas.map((row) => (
            <li key={row.symbol} className="tabular-nums">
              {row.symbol}: book {row.ledger_qty} vs broker {row.broker_qty}
            </li>
          ))}
        </ul>
      ) : null}
      {error ? (
        <p className="text-sm text-negative" role="alert">
          {error}
        </p>
      ) : null}
      {fixed ? <p className="text-sm text-muted-foreground">{fixed}</p> : null}
      <div className="flex flex-wrap gap-2">
        <Button type="submit" size="sm" disabled={busy} variant="outline">
          {busy ? "Working…" : "Scan"}
        </Button>
        {needsFix ? (
          <Button type="button" size="sm" disabled={busy} onClick={onFix}>
            Fix now
          </Button>
        ) : null}
      </div>
    </form>
  );
}
