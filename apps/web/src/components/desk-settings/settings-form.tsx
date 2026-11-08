"use client";

import { useMemo, useState } from "react";

import { Banner, Card, ConfirmAction, Muted, StaffOnly, useDeskPage } from "@/components/ops/shared";
import { Row, Table, Td, Th } from "@/components/desk/ui";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { useIsStaff } from "@/components/shell/staff-context";
import { errorText, postDesk } from "@/lib/desk-ops/client";

/** `GET /desk/settings` (Accept: application/json). */
interface Spec {
  key: string;
  label: string;
  group: string;
  kind: string;
  help: string;
  minimum: number | null;
  maximum: number | null;
  choices: string[];
  unit: string;
}

interface Effective {
  spec: Spec;
  value: unknown;
  text: string;
  source: string;
}

interface Risk {
  nav: number;
  nav_short: string;
  have_nav: boolean;
  derived: boolean;
  rows: { key: string; value: string; short: string; means: string }[];
  problems: string[];
}

interface SettingsView {
  eff: Record<string, Effective>;
  groups: string[];
  locked: { key: string; value: unknown; why: string }[];
  history: { key: string; old_value: string | null; new_value: string; changed_at: string; note: string | null }[];
  dry_run: boolean;
  risk: Risk | null;
}

// Execution / auto-execute / dry-run switches never become editable from the web, whatever the desk lists.
const NEVER_EDITABLE = /EXECUT|AUTO_EXEC|DRY_RUN|INTRADAY_ENABLED|OPTIONS_ENABLED|LIVE_ORDERS/;

function initial(e: Effective): string {
  return e.spec.kind === "bool" ? (e.value === true ? "true" : "false") : e.text;
}

export function SettingsForm() {
  const staff = useIsStaff();
  const { data, error, reload } = useDeskPage<SettingsView>("/settings");
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [note, setNote] = useState("");
  const [notice, setNotice] = useState<{ ok: boolean; text: string } | null>(null);

  const changed = useMemo(() => {
    if (!data) return [] as string[];
    return Object.entries(data.eff)
      .filter(([k, e]) => !NEVER_EDITABLE.test(k) && draft[k] !== undefined && draft[k] !== initial(e))
      .map(([k]) => k);
  }, [data, draft]);

  if (!staff) return <StaffOnly what="Desk settings" />;
  if (error && !data) return <Banner tone="bad" tag="unavailable">{error}</Banner>;
  if (!data) return <Muted>Loading settings…</Muted>;

  const save = async () => {
    // Only the changed keys; `_form_bools` names just the changed booleans so nothing else is touched.
    const fields: Record<string, string> = { note };
    const bools: string[] = [];
    for (const key of changed) {
      const e = data.eff[key];
      if (!e) continue;
      if (e.spec.kind === "bool") {
        bools.push(key);
        if (draft[key] === "true") fields[key] = "true";
      } else {
        fields[key] = draft[key] ?? "";
      }
    }
    fields._form_bools = bools.join(",");
    try {
      const res = await postDesk("/settings", fields);
      setNotice({ ok: true, text: `${res.message}. Applied immediately; no restart needed.` });
      setNote("");
      setDraft({});
    } catch (e) {
      setNotice({ ok: false, text: `${errorText(e)} Nothing was written; settings are all-or-nothing.` });
    }
    await reload();
  };

  const resetAll = async () => {
    try {
      const res = await postDesk("/settings/reset", {});
      setNotice({ ok: true, text: res.message });
    } catch (e) {
      setNotice({ ok: false, text: errorText(e) });
    }
    await reload();
  };

  return (
    <div className="flex flex-col gap-5">
      {notice ? (
        <Banner tone={notice.ok ? "ok" : "bad"} tag={notice.ok ? "saved" : "rejected"}>
          {notice.text}
        </Banner>
      ) : null}
      <Banner tone="info" tag="how this works">
        Values resolve <b>database → .env → code default</b>. Saving writes an override that beats <code>.env</code>{" "}
        and takes effect on the next plan, without a restart. These change how the desk sizes and stops trades, so
        every save asks for confirmation and is recorded below.
      </Banner>

      {data.risk ? (
        <Card title="Risk limits">
          <Muted className="mb-3">
            Not editable from this page. They bound how much can be lost and how fast orders can leave, so they are
            set in <code>.env</code> and take a restart.
          </Muted>
          {data.risk.have_nav ? (
            <>
              <Muted className="mb-2">
                Against the latest stored NAV of <b>₹{Math.round(data.risk.nav).toLocaleString("en-IN")}</b> (
                {data.risk.nav_short}) — the figures the gateway enforces.
              </Muted>
              <dl className="grid gap-2 text-sm">
                {data.risk.rows.map((r) => (
                  <div key={r.key} className="grid gap-1 sm:grid-cols-[16rem_10rem_1fr]">
                    <dt>
                      <code className="text-xs">{r.key}</code>
                    </dt>
                    <dd className="font-semibold tabular-nums">
                      {r.value}
                      {r.short ? <span className="ml-1 text-xs font-normal text-muted-foreground">({r.short})</span> : null}
                    </dd>
                    <dd className="text-xs text-muted-foreground">{r.means}</dd>
                  </div>
                ))}
              </dl>
              {data.risk.problems.length ? (
                <div className="mt-3">
                  <Banner tone="bad">
                    <b>These limits contradict the strategy&apos;s own sizing.</b>
                    <ul className="ml-4 mt-1 list-disc">
                      {data.risk.problems.map((p) => (
                        <li key={p}>{p}</li>
                      ))}
                    </ul>
                  </Banner>
                </div>
              ) : null}
            </>
          ) : (
            <Muted>No stored snapshot yet, so these cannot be shown in rupees.</Muted>
          )}
        </Card>
      ) : null}

      {data.groups.map((group) => (
        <Card key={group} title={group}>
          <div className="flex flex-col divide-y divide-border/60">
            {Object.entries(data.eff)
              .filter(([, e]) => e.spec.group === group)
              .map(([key, e]) => {
                const locked = NEVER_EDITABLE.test(key);
                const value = draft[key] ?? initial(e);
                return (
                  <div key={key} className="grid gap-2 py-3 md:grid-cols-[1fr_14rem_10rem_7rem] md:items-center">
                    <div className="min-w-0">
                      <label htmlFor={`s-${key}`} className="text-sm font-medium">
                        {e.spec.label}
                      </label>
                      {e.spec.help ? <Muted>{e.spec.help}</Muted> : null}
                      <code className="text-[11px] text-muted-foreground">{key}</code>
                    </div>
                    <div>
                      {e.spec.kind === "bool" ? (
                        <div className="flex items-center gap-2">
                          <Switch
                            id={`s-${key}`}
                            disabled={locked}
                            checked={value === "true"}
                            onCheckedChange={(on) => setDraft((d) => ({ ...d, [key]: on ? "true" : "false" }))}
                          />
                          <span className="text-xs text-muted-foreground">{value === "true" ? "on" : "off"}</span>
                        </div>
                      ) : e.spec.kind === "choice" ? (
                        <Select
                          id={`s-${key}`}
                          disabled={locked}
                          value={value}
                          onChange={(ev) => setDraft((d) => ({ ...d, [key]: ev.target.value }))}
                        >
                          {e.spec.choices.map((c) => (
                            <option key={c} value={c}>
                              {c}
                            </option>
                          ))}
                        </Select>
                      ) : (
                        <Input
                          id={`s-${key}`}
                          disabled={locked}
                          inputMode="decimal"
                          value={value}
                          onChange={(ev) => setDraft((d) => ({ ...d, [key]: ev.target.value }))}
                        />
                      )}
                    </div>
                    <Muted>
                      {e.spec.kind === "intpair"
                        ? "two numbers, min,max"
                        : e.spec.minimum !== null
                          ? `range ${e.spec.minimum}–${e.spec.maximum}`
                          : ""}
                      {e.spec.unit ? ` ${e.spec.unit}` : ""}
                    </Muted>
                    <div>
                      <Badge variant={e.source === "database" ? "accent" : "neutral"}>{e.source}</Badge>
                    </div>
                  </div>
                );
              })}
          </div>
        </Card>
      ))}

      <Card>
        <label htmlFor="settings-note" className="text-sm font-medium">
          Why are you changing this?
        </label>
        <Muted className="mb-2">Stored with the change. Months from now this is the only record of why a threshold moved.</Muted>
        <Input id="settings-note" value={note} placeholder="optional but worth it" onChange={(e) => setNote(e.target.value)} />
        <div className="mt-3 flex flex-wrap items-center gap-3">
          <ConfirmAction
            label={changed.length ? `Save ${changed.length} change${changed.length === 1 ? "" : "s"}` : "No changes"}
            confirmLabel="Save settings"
            variant="primary"
            disabled={!changed.length}
            warning={
              <>
                Change <b>{changed.join(", ")}</b>. The next plan the desk builds uses these values.
              </>
            }
            onConfirm={save}
          />
          <Muted>Validated as a set: if any value is rejected, nothing is written.</Muted>
        </div>
      </Card>

      <Card title="Not editable here">
        <div className="flex flex-col divide-y divide-border/60 text-sm">
          {data.locked.map((l) => (
            <div key={l.key} className="grid gap-1 py-2 md:grid-cols-[14rem_8rem_1fr]">
              <code className="text-xs">{l.key}</code>
              <b>{String(l.value)}</b>
              <span className="text-xs text-muted-foreground">{l.why}</span>
            </div>
          ))}
        </div>
        <Muted className="mt-3">
          Credentials are never stored in the database and never rendered. The safety switches stay in <code>.env</code>{" "}
          deliberately: enabling live orders is an act on the machine that runs the system, not a click in a browser.
        </Muted>
      </Card>

      <Card title="Change history">
        {data.history.length ? (
          <>
            <Table
              caption="Change history"
              head={
                <>
                  <Th>When</Th>
                  <Th>Setting</Th>
                  <Th>From</Th>
                  <Th>To</Th>
                  <Th>Note</Th>
                </>
              }
            >
              {data.history.map((h) => (
                <Row key={`${h.changed_at}-${h.key}`}>
                  <Td>{h.changed_at}</Td>
                  <Td>
                    <code className="text-xs">{h.key}</code>
                  </Td>
                  <Td>{h.old_value ?? "—"}</Td>
                  <Td>
                    <b>{h.new_value}</b>
                  </Td>
                  <Td>{h.note || "—"}</Td>
                </Row>
              ))}
            </Table>
            <div className="mt-3">
              <ConfirmAction
                label="Reset all overrides to .env"
                confirmLabel="Reset all"
                variant="destructive"
                warning="Removes every database override; each setting returns to .env or its code default. The reset is recorded."
                onConfirm={resetAll}
              />
            </div>
          </>
        ) : (
          <Muted>No settings have been changed yet. Everything runs on .env and the code defaults.</Muted>
        )}
      </Card>
    </div>
  );
}
