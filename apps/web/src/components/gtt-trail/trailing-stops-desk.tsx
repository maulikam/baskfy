"use client";

import { useCallback, useEffect, useState } from "react";

import { Row, Table, Td, Th, pct, price } from "@/components/desk/ui";
import { useIsStaff } from "@/components/shell/staff-context";
import { Button } from "@/components/ui/button";
import { DeskError } from "@/lib/desk-actions/client";
import {
  STRATEGY_LABELS,
  fetchTrailJournal,
  fetchTrailPreview,
  fetchTrailSettings,
  policyProblem,
  saveTrailSettings,
  type TrailJournalRow,
  type TrailPolicy,
  type TrailPreview,
  type TrailSettings,
} from "@/lib/gtt-trail/desk";
import { cn } from "@/lib/utils";

const BOX = "rounded-xl border p-3.5 text-sm";
const WARN = "border-warning/35 bg-warning-muted";
const BAD = "border-negative/40 bg-negative-muted";
const OK = "border-positive/40 bg-positive-muted";

const FIELDS: { key: keyof TrailPolicy; label: string; hint: string }[] = [
  { key: "breakeven_after_pct", label: "Breakeven after %", hint: "Move the stop to entry once the highest close is up this much." },
  { key: "breakeven_buffer_pct", label: "Buffer above entry %", hint: "The breakeven stop sits this far above entry, for costs." },
  { key: "trail_after_pct", label: "Trail after %", hint: "Start trailing once the highest close is up this much." },
  { key: "trail_pct", label: "Trail %", hint: "The stop follows this far under the highest close since entry." },
  { key: "min_step_pct", label: "Min step %", hint: "Skip a raise smaller than this, so the GTT is not churned." },
];

function message(error: unknown): string {
  if (error instanceof DeskError) return `${error.status}: ${error.message}`;
  return error instanceof Error ? error.message : String(error);
}

export function TrailingStopsDesk() {
  const isStaff = useIsStaff();
  const [settings, setSettings] = useState<TrailSettings | null>(null);
  const [draft, setDraft] = useState<TrailPolicy[]>([]);
  const [preview, setPreview] = useState<TrailPreview | null>(null);
  const [journal, setJournal] = useState<TrailJournalRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);

  const reload = useCallback(() => setReloadKey((k) => k + 1), []);

  useEffect(() => {
    if (!isStaff) return;
    let live = true;
    Promise.all([fetchTrailSettings(), fetchTrailPreview(), fetchTrailJournal(50)])
      .then(([s, p, j]) => {
        if (!live) return;
        setSettings(s);
        setDraft(s.policies);
        setPreview(p);
        setJournal(j.rows);
        setError(null);
      })
      .catch((e: unknown) => {
        if (live) setError(message(e));
      });
    return () => {
      live = false;
    };
  }, [isStaff, reloadKey]);

  if (!isStaff) {
    return <p className="text-sm text-muted-foreground">Trailing stops is an operator page. Nothing to show here.</p>;
  }

  function update(strategy: string, patch: Partial<TrailPolicy>) {
    setSaved(null);
    setDraft((rows) => rows.map((r) => (r.strategy === strategy ? { ...r, ...patch } : r)));
  }

  const problems = draft.map((p) => [p.strategy, policyProblem(p)] as const).filter(([, why]) => why);

  async function onSave() {
    if (saving || problems.length) return;
    setSaving(true);
    setError(null);
    try {
      const s = await saveTrailSettings(draft);
      setSettings(s);
      setDraft(s.policies);
      setSaved("Saved. The preview below is recomputed with the new policy.");
      reload();
    } catch (e) {
      setError(message(e));
    } finally {
      setSaving(false);
    }
  }

  const master = settings?.master_enabled ?? false;
  const dryRun = settings?.dry_run ?? false;
  const wired = new Set(settings?.wired ?? []);

  return (
    <div className="flex flex-col gap-5">
      <p className={cn(BOX, master ? (dryRun ? WARN : BAD) : WARN)} role="status">
        <b>{master ? (dryRun ? "Master flag on, DRY_RUN on. " : "Master flag on, live. ") : "Master flag off. "}</b>
        {master
          ? dryRun
            ? "The evening job simulates every raise through the gateway and changes nothing at the broker."
            : "After each weekday close the desk raises the resting GTT trigger of every position whose strategy is enabled below. A stop only ever moves up."
          : "Changes here are saved but nothing is sent: GTTs are modified only when BASKFY_GTT_TRAIL_ENABLED is true on the desk, which is Maulik's switch. The job still journals what it would have done."}
      </p>
      {error ? <p className={cn(BOX, BAD)}>Could not read the desk: {error}</p> : null}
      {saved ? <p className={cn(BOX, OK)}>{saved}</p> : null}

      <section className="flex flex-col gap-3" aria-labelledby="trail-policy">
        <h2 id="trail-policy" className="text-lg font-semibold">
          Policy by strategy
        </h2>
        <Table
          caption="Trailing-stop policy"
          head={
            <>
              <Th>Strategy</Th>
              <Th>Enabled</Th>
              {FIELDS.map((f) => (
                <Th key={f.key} align="right">
                  {f.label}
                </Th>
              ))}
            </>
          }
        >
          {draft.map((p) => (
            <Row key={p.strategy}>
              <Td>
                <span className="font-medium">{STRATEGY_LABELS[p.strategy] ?? p.strategy}</span>
                {wired.has(p.strategy) ? null : (
                  <span className="block text-xs text-muted-foreground">Saved only: no stored entry or GTT to act on yet.</span>
                )}
              </Td>
              <Td>
                <input
                  type="checkbox"
                  aria-label={`Enable trailing stops for ${p.strategy}`}
                  checked={p.enabled}
                  onChange={(e) => update(p.strategy, { enabled: e.target.checked })}
                />
              </Td>
              {FIELDS.map((f) => (
                <Td key={f.key} align="right">
                  <input
                    type="number"
                    step="0.5"
                    title={f.hint}
                    aria-label={`${f.label} for ${p.strategy}`}
                    className="w-20 rounded-md border border-input bg-background px-2 py-1 text-right"
                    value={String(p[f.key])}
                    onChange={(e) => update(p.strategy, { [f.key]: Number(e.target.value) })}
                  />
                </Td>
              ))}
            </Row>
          ))}
        </Table>
        {problems.length ? (
          <p className={cn(BOX, WARN)}>
            {problems.map(([s, why]) => (
              <span key={s} className="block">
                {STRATEGY_LABELS[s] ?? s}: {why}
              </span>
            ))}
          </p>
        ) : null}
        <div>
          <Button onClick={() => void onSave()} disabled={saving || problems.length > 0}>
            {saving ? "Saving…" : "Save policy"}
          </Button>
        </div>
        <p className="text-xs text-muted-foreground">
          Stop = the higher of the resting stop, entry plus buffer once breakeven is reached, and the highest close since
          entry less the trail once trailing starts — floored to the tick, kept 0.5% under the last close, never lowered,
          and read only from published daily bars of completed sessions.
        </p>
      </section>

      <section className="flex flex-col gap-3" aria-labelledby="trail-preview">
        <h2 id="trail-preview" className="text-lg font-semibold">
          Preview{preview ? ` · bars to ${preview.as_of}` : ""}
        </h2>
        {preview && preview.rows.length === 0 ? (
          <p className="text-sm text-muted-foreground">No open position with a stop on record.</p>
        ) : null}
        {preview && preview.rows.length ? (
          <Table
            caption="Proposed stops"
            head={
              <>
                <Th>Stock</Th>
                <Th>Strategy</Th>
                <Th align="right">Qty</Th>
                <Th align="right">Entry</Th>
                <Th align="right">Last close</Th>
                <Th align="right">Current stop</Th>
                <Th align="right">Proposed stop</Th>
                <Th align="right">Locked in</Th>
                <Th>Would change</Th>
                <Th>Reason</Th>
              </>
            }
          >
            {preview.rows.map((r) => (
              <Row key={`${r.strategy}:${r.position_id}`}>
                <Td>
                  <span className="font-medium">{r.symbol}</span>
                </Td>
                <Td tone="text-muted-foreground">{STRATEGY_LABELS[r.strategy] ?? r.strategy}</Td>
                <Td align="right">{r.qty.toLocaleString("en-IN")}</Td>
                <Td align="right">{price(r.entry)}</Td>
                <Td align="right">{price(r.last_close)}</Td>
                <Td align="right">{price(r.current_stop)}</Td>
                <Td align="right" tone={r.would_change ? "text-positive" : ""}>
                  {price(r.proposed_stop)}
                </Td>
                <Td align="right" tone={r.locked_pct !== null && r.locked_pct >= 0 ? "text-positive" : "text-negative"}>
                  {pct(r.locked_pct, { sign: true })}
                </Td>
                <Td>{r.would_change ? (r.policy_enabled ? "Yes" : "Yes (policy off)") : "No"}</Td>
                <Td tone="text-muted-foreground">{r.blocker ? `${r.reason} · ${r.blocker}` : r.reason}</Td>
              </Row>
            ))}
          </Table>
        ) : null}
      </section>

      <section className="flex flex-col gap-3" aria-labelledby="trail-journal">
        <h2 id="trail-journal" className="text-lg font-semibold">
          Journal
        </h2>
        {journal.length === 0 ? (
          <p className="text-sm text-muted-foreground">Nothing journalled yet.</p>
        ) : (
          <Table
            caption="Trailing-stop journal"
            head={
              <>
                <Th>When</Th>
                <Th>Stock</Th>
                <Th>Strategy</Th>
                <Th align="right">Old</Th>
                <Th align="right">New</Th>
                <Th>Outcome</Th>
                <Th>Detail</Th>
              </>
            }
          >
            {journal.map((j) => (
              <Row key={j.id}>
                <Td tone="text-muted-foreground">{j.at ? new Date(j.at).toLocaleString("en-IN") : "–"}</Td>
                <Td>{j.symbol ?? "–"}</Td>
                <Td tone="text-muted-foreground">{j.strategy ?? "–"}</Td>
                <Td align="right">{price(j.old_stop)}</Td>
                <Td align="right">{price(j.new_stop)}</Td>
                <Td>{j.outcome ?? "–"}</Td>
                <Td tone="text-muted-foreground">{[j.reason, j.detail].filter(Boolean).join(" · ")}</Td>
              </Row>
            ))}
          </Table>
        )}
      </section>
    </div>
  );
}
