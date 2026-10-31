"use client";

import { Check, X } from "lucide-react";
import Link from "next/link";
import type { Route } from "next";
import { useId, useState } from "react";

import { NumberField } from "@/components/opportunities/fields";
import { ErrorPanel } from "@/components/opportunities/states";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { addToWatchlist, isAbort, validateSwing } from "@/lib/opportunities/client";
import { toProblem } from "@/lib/opportunities/hooks";
import { ladderPositions } from "@/lib/opportunities/payoff";
import type {
  ProblemBody,
  SwingOpportunity,
  ValidateResponse,
} from "@/lib/opportunities/types";
import {
  SETUP_LABEL,
  evidenceValue,
  formatInr,
  formatPct,
  formatSessionDate,
  label,
  optionsHref,
} from "@/lib/opportunities/view";
import { formatNumber } from "@/lib/format";

function Evidence({ opportunity }: { opportunity: SwingOpportunity }) {
  return (
    <section aria-label="Qualification evidence" className="space-y-2">
      <h4 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Qualification evidence</h4>
      <ul className="space-y-1 text-sm">
        {opportunity.evidence.map((item) => (
          <li key={item.key} className="flex items-start gap-2">
            {item.passed ? (
              <Check className="mt-0.5 size-4 shrink-0 text-positive" aria-label="passed" />
            ) : (
              <X className="mt-0.5 size-4 shrink-0 text-negative" aria-label="failed" />
            )}
            <span className="min-w-0">
              {item.label}:{" "}
              <span className="tabular-nums font-medium">{evidenceValue(item.value, item.unit)}</span>
              {item.threshold !== null ? (
                <span className="text-muted-foreground"> vs {evidenceValue(item.threshold, item.unit)}</span>
              ) : null}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

export function ScoreBreakdown({ score }: { score: SwingOpportunity["score"] }) {
  return (
    <section aria-label="Score breakdown" className="space-y-2">
      <h4 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
        Score {formatNumber(score.total, { decimals: 1 })}{" "}
        <span className="normal-case tracking-normal">— rule-based score, not a probability</span>
      </h4>
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-xs text-muted-foreground">
            <th className="py-1 pr-2 font-medium">Component</th>
            <th className="py-1 pr-2 text-right font-medium">Value</th>
            <th className="py-1 pr-2 text-right font-medium">Weight</th>
            <th className="py-1 text-right font-medium">Contribution</th>
          </tr>
        </thead>
        <tbody>
          {score.components.map((component) => (
            <tr key={component.key} className="border-t border-border/40">
              <td className="py-1 pr-2">{component.label}</td>
              <td className="py-1 pr-2 text-right tabular-nums">{formatNumber(component.value, { decimals: 2 })}</td>
              <td className="py-1 pr-2 text-right tabular-nums">{formatNumber(component.weight, { decimals: 2 })}</td>
              <td className="py-1 text-right tabular-nums">{formatNumber(component.contribution, { decimals: 2 })}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function Ladder({ opportunity }: { opportunity: SwingOpportunity }) {
  const pos = ladderPositions(
    opportunity.invalidation,
    opportunity.entry.price,
    opportunity.targets.map((t) => t.price),
  );
  const marks = [
    { key: "inv", at: pos.invalidation, text: `Invalidation ${formatInr(opportunity.invalidation)}`, tone: "bg-negative" },
    { key: "entry", at: pos.entry, text: `Entry ${formatInr(opportunity.entry.price)}`, tone: "bg-foreground" },
    ...opportunity.targets.map((target, i) => ({
      key: `t${i}`,
      at: pos.targets[i] ?? 100,
      text: `${target.label} ${formatInr(target.price)} (${formatNumber(target.r_multiple, { decimals: 1 })}R)`,
      tone: "bg-positive",
    })),
  ];
  return (
    <section aria-label="Price ladder" className="space-y-2">
      <h4 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Entry, invalidation and targets</h4>
      <div className="relative mx-2 h-2 rounded-full bg-muted">
        <div
          className="absolute h-2 rounded-l-full bg-negative-muted"
          style={{ left: `${pos.invalidation}%`, width: `${Math.max(0, pos.entry - pos.invalidation)}%` }}
        />
        <div
          className="absolute h-2 rounded-r-full bg-positive-muted"
          style={{ left: `${pos.entry}%`, width: `${Math.max(0, Math.max(...pos.targets, pos.entry) - pos.entry)}%` }}
        />
        {marks.map((mark) => (
          <span
            key={mark.key}
            className={`absolute top-1/2 size-3 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-card ${mark.tone}`}
            style={{ left: `${mark.at}%` }}
            title={mark.text}
          />
        ))}
      </div>
      <ul className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
        {marks.map((mark) => (
          <li key={mark.key} className="tabular-nums">
            {mark.text}
          </li>
        ))}
      </ul>
      <p className="text-xs text-muted-foreground">
        Entry basis: {opportunity.entry.basis}. Trigger: {opportunity.trigger.condition} at{" "}
        {formatInr(opportunity.trigger.level)}
        {opportunity.trigger.triggered_on ? ` (triggered ${formatSessionDate(opportunity.trigger.triggered_on)})` : ""}.
        Risk {formatInr(opportunity.risk_per_share)}/share, reward {formatInr(opportunity.reward_per_share)}/share.
      </p>
    </section>
  );
}

function ValidatePanel({
  opportunity,
  universe,
  horizonMax,
}: {
  opportunity: SwingOpportunity;
  universe: string;
  horizonMax: number;
}) {
  const id = useId();
  const [start, setStart] = useState("2016-01-01");
  const [end, setEnd] = useState(opportunity.data_as_of);
  const [slippage, setSlippage] = useState<number | null>(10);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ValidateResponse | null>(null);
  const [error, setError] = useState<ProblemBody | null>(null);

  async function run() {
    setBusy(true);
    setError(null);
    try {
      setResult(
        await validateSwing({
          setup: opportunity.setup,
          universe,
          start,
          end,
          horizon_max: horizonMax,
          slippage_bps: slippage ?? 0,
        }),
      );
    } catch (caught) {
      if (!isAbort(caught)) setError(toProblem(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section aria-label="Historical signal test" className="space-y-2 rounded-md border border-border/60 p-3" data-testid="validate-panel">
      <h4 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
        Historical signal test (point-in-time, after costs/slippage) — {label(SETUP_LABEL, opportunity.setup)}
      </h4>
      <div className="flex flex-wrap items-end gap-3">
        <div className="flex flex-col gap-1 text-xs text-muted-foreground">
          <label htmlFor={`${id}-from`}>From</label>
          <Input id={`${id}-from`} type="date" value={start} onChange={(e) => setStart(e.target.value)} className="w-40" />
        </div>
        <div className="flex flex-col gap-1 text-xs text-muted-foreground">
          <label htmlFor={`${id}-to`}>To</label>
          <Input id={`${id}-to`} type="date" value={end} onChange={(e) => setEnd(e.target.value)} className="w-40" />
        </div>
        <div className="w-32">
          <NumberField label="Slippage" suffix="bps" min={0} value={slippage} onChange={setSlippage} />
        </div>
        <Button size="sm" variant="outline" disabled={busy} onClick={() => void run()}>
          {busy ? "Testing…" : "Run test"}
        </Button>
      </div>
      {error ? <ErrorPanel problem={error} /> : null}
      {result ? (
        result.available ? (
          <div className="space-y-2 text-sm">
            <dl className="grid grid-cols-2 gap-x-4 gap-y-1 sm:grid-cols-4">
              <dt className="text-muted-foreground">Trades</dt>
              <dd className="tabular-nums">{formatNumber(result.trades)} ({formatNumber(result.wins)} wins)</dd>
              <dt className="text-muted-foreground">Hit rate</dt>
              <dd className="tabular-nums">{formatPct(result.hit_rate_pct, 1)}</dd>
              <dt className="text-muted-foreground">Avg return</dt>
              <dd className="tabular-nums">{formatPct(result.avg_return_pct, 2, true)}</dd>
              <dt className="text-muted-foreground">Avg R</dt>
              <dd className="tabular-nums">{formatNumber(result.avg_r, { decimals: 2, signed: true })}</dd>
              <dt className="text-muted-foreground">Expectancy</dt>
              <dd className="tabular-nums">{formatNumber(result.expectancy_r, { decimals: 2, signed: true, suffix: "R" })}</dd>
              <dt className="text-muted-foreground">Worst adverse</dt>
              <dd className="tabular-nums">{formatNumber(result.max_adverse_r, { decimals: 2, suffix: "R" })}</dd>
            </dl>
            {result.by_year.length > 0 ? (
              <p className="text-xs text-muted-foreground">
                By year:{" "}
                {result.by_year
                  .map((y) => `${y.year} ${y.trades}× ${formatNumber(y.avg_r, { decimals: 2, signed: true })}R`)
                  .join(" · ")}
              </p>
            ) : null}
            <ul className="list-disc pl-4 text-xs text-muted-foreground">
              {result.assumptions.map((a) => (
                <li key={a}>{a}</li>
              ))}
            </ul>
            <p className="text-xs text-muted-foreground">
              Period {formatSessionDate(result.period.start)} – {formatSessionDate(result.period.end)}. Past signal
              behaviour does not predict future results.
            </p>
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">Test unavailable: {result.reason ?? "no reason given"}.</p>
        )
      ) : null}
    </section>
  );
}

function WatchlistButton({ symbol }: { symbol: string }) {
  const [state, setState] = useState<"idle" | "busy" | "added" | "exists" | "error">("idle");
  async function add() {
    setState("busy");
    try {
      setState(await addToWatchlist(symbol));
    } catch (_error) {
      setState("error");
    }
  }
  const text =
    state === "added" ? "Added to watchlist" : state === "exists" ? "Already on watchlist" : state === "busy" ? "Adding…" : "Add to watchlist";
  return (
    <span className="inline-flex items-center gap-2">
      <Button size="sm" variant="outline" disabled={state !== "idle" && state !== "error"} onClick={() => void add()}>
        {text}
      </Button>
      {state === "error" ? <span className="text-xs text-negative">Could not add</span> : null}
    </span>
  );
}

export function SwingDetail({
  opportunity,
  universe,
  horizonMax,
}: {
  opportunity: SwingOpportunity;
  universe: string;
  horizonMax: number;
}) {
  const [showValidate, setShowValidate] = useState(false);
  return (
    <div className="grid gap-5 p-4 lg:grid-cols-2" data-testid={`detail-${opportunity.symbol}`}>
      <div className="space-y-5">
        <Evidence opportunity={opportunity} />
        <Ladder opportunity={opportunity} />
      </div>
      <div className="space-y-5">
        <ScoreBreakdown score={opportunity.score} />
        {opportunity.limitations.length > 0 ? (
          <section aria-label="Limitations" className="space-y-1">
            <h4 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Limitations</h4>
            <ul className="list-disc space-y-0.5 pl-4 text-sm text-muted-foreground">
              {opportunity.limitations.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </section>
        ) : null}
        <div className="flex flex-wrap items-center gap-2">
          <WatchlistButton symbol={opportunity.symbol} />
          <Button size="sm" variant="outline" asChild>
            <Link href={optionsHref(opportunity.symbol, "BULLISH") as Route}>Analyze options</Link>
          </Button>
          <Button size="sm" variant="outline" onClick={() => setShowValidate((v) => !v)} aria-expanded={showValidate}>
            Historical signal test
          </Button>
        </div>
      </div>
      {showValidate ? (
        <div className="lg:col-span-2">
          <ValidatePanel opportunity={opportunity} universe={universe} horizonMax={horizonMax} />
        </div>
      ) : null}
    </div>
  );
}
