"use client";

import { useState } from "react";

import { Banner, Card, ConfirmAction, Field, Muted, StaffOnly, fmtInr, useDeskPage } from "@/components/ops/shared";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { useIsStaff } from "@/components/shell/staff-context";
import { errorText, postDesk } from "@/lib/desk-ops/client";

/** `GET /desk/lifecycle/data`. Money fields arrive as decimal strings. */
interface LifecycleRow {
  sleeve: string;
  position_id: number;
  symbol: string;
  entry_date: string;
  entry_avg: string | null;
  filled_qty: number;
  open_qty: number;
  initial_stop: string | null;
  stop: string | null;
  rupee_risk: string | null;
  stop_state: string;
  stop_qty: number | null;
  gtt_id: string | null;
  exit_rule: string;
  next_action: string;
  overdue: boolean;
  simulated: boolean;
  issues: string[];
}

interface LifecycleView {
  rows: LifecycleRow[];
  unresolved: number;
  gtt_checked: boolean;
  rules: Record<string, string>;
  now: string;
  error: string | null;
}

// The desk's `resolved`: armed and nothing open against it.
const resolved = (r: LifecycleRow) => r.stop_state === "ARMED" && r.issues.length === 0;

const EMPTY_FORM = { sleeve: "swing", symbol: "", quantity: "", avg_cost: "", gtt_id: "", setup: "", note: "" };

function AdoptForm({ onDone }: { onDone: () => Promise<void> }) {
  const [form, setForm] = useState(EMPTY_FORM);
  const [boughtByHand, setBoughtByHand] = useState(false);
  const [notice, setNotice] = useState<{ ok: boolean; text: string } | null>(null);
  const set = (k: keyof typeof EMPTY_FORM, v: string) => setForm((f) => ({ ...f, [k]: v }));
  const qty = Number(form.quantity);
  const cost = Number(form.avg_cost);
  const valid = form.symbol.trim() !== "" && Number.isInteger(qty) && qty >= 1 && Number.isFinite(cost) && cost > 0;

  const adopt = async () => {
    try {
      const res = await postDesk("/lifecycle/adopt", { ...form, confirm: "true" });
      setNotice({ ok: true, text: res.message });
      setForm(EMPTY_FORM);
      setBoughtByHand(false);
    } catch (e) {
      setNotice({ ok: false, text: errorText(e) });
    }
    await onDone();
  };

  return (
    <Card title="Adopt a holding — explicitly, on your numbers">
      <Muted className="mb-3">
        A holding you bought in the Kite app becomes a strategy position on the cost and quantity you state. Name the
        stop you armed by hand to link it; leave it blank and the strategy arms one through the gateway at its own stop
        distance. A name the strategy already holds is refused.
      </Muted>
      {notice ? (
        <div className="mb-3">
          <Banner tone={notice.ok ? "ok" : "bad"} tag={notice.ok ? "adopted" : "refused"}>
            {notice.text}
          </Banner>
        </div>
      ) : null}
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Field id="adopt-sleeve" label="Strategy">
          <Select id="adopt-sleeve" value={form.sleeve} onChange={(e) => set("sleeve", e.target.value)}>
            <option value="swing">swing</option>
            <option value="twt">twt</option>
            <option value="vbt">vbt</option>
          </Select>
        </Field>
        <Field id="adopt-symbol" label="Symbol">
          <Input id="adopt-symbol" value={form.symbol} placeholder="NSE symbol" onChange={(e) => set("symbol", e.target.value.toUpperCase())} />
        </Field>
        <Field id="adopt-quantity" label="Quantity">
          <Input id="adopt-quantity" type="number" min={1} value={form.quantity} onChange={(e) => set("quantity", e.target.value)} />
        </Field>
        <Field id="adopt-avg-cost" label="Avg cost">
          <Input id="adopt-avg-cost" type="number" min={0.01} step={0.01} value={form.avg_cost} onChange={(e) => set("avg_cost", e.target.value)} />
        </Field>
        <Field id="adopt-stop-trigger-id-optional" label="Stop trigger id (optional)">
          <Input id="adopt-stop-trigger-id-optional" value={form.gtt_id} onChange={(e) => set("gtt_id", e.target.value)} />
        </Field>
        <Field id="adopt-swing-setup" label="Swing setup">
          <Select id="adopt-swing-setup" value={form.setup} onChange={(e) => set("setup", e.target.value)}>
            <option value="">—</option>
            <option value="FLAG">FLAG</option>
            <option value="EP">EP</option>
          </Select>
        </Field>
        <Field id="adopt-note" label="Note" className="sm:col-span-2">
          <Input id="adopt-note" value={form.note} placeholder="why this strategy" onChange={(e) => set("note", e.target.value)} />
        </Field>
      </div>
      <label className="mt-3 flex items-center gap-2 text-sm">
        <input type="checkbox" checked={boughtByHand} onChange={(e) => setBoughtByHand(e.target.checked)} />
        I bought this by hand and want this strategy to manage it
      </label>
      <div className="mt-3">
        <ConfirmAction
          label="Adopt"
          confirmLabel="Adopt it"
          variant="primary"
          disabled={!valid || !boughtByHand}
          warning={
            <>
              Adopt <b>{qty} {form.symbol || "—"}</b> at {fmtInr(cost, 2)} into <b>{form.sleeve}</b>.{" "}
              {form.gtt_id.trim()
                ? `Links stop trigger #${form.gtt_id.trim()}.`
                : "No stop named: the strategy arms one through the gateway now."}
            </>
          }
          onConfirm={adopt}
        />
      </div>
    </Card>
  );
}

export function LifecycleBoard() {
  const staff = useIsStaff();
  const { data, error, reload } = useDeskPage<LifecycleView>("/lifecycle/data", () => 10000);
  if (!staff) return <StaffOnly what="Lifecycle" />;
  if (error && !data) return <Banner tone="bad" tag="unavailable">{error}</Banner>;
  if (!data) return <Muted>Loading positions…</Muted>;

  return (
    <div className="flex flex-col gap-5">
      <Muted>
        One row per open position. The stop column is the <b>broker&apos;s</b> word
        {data.gtt_checked ? "" : " — not checked: no Kite session, so every stop reads UNVERIFIED until a login"}. A
        stop that fired is not a fill: the row stays unresolved until the position closes. Refreshed every ten seconds.
      </Muted>
      {data.error ? <Banner tone="bad">The page could not read the positions: {data.error}</Banner> : null}
      <h2 className="text-base font-semibold">
        {data.rows.length} open
        {data.unresolved ? <span className="text-negative">, {data.unresolved} unresolved</span> : null}
      </h2>
      <div className="overflow-hidden rounded-xl border border-border/70 bg-card">
        <div className="overflow-x-auto">
          <table className="w-full text-sm tabular-nums">
            <caption className="sr-only">Open strategy positions</caption>
            <thead>
              <tr className="border-b border-border bg-muted/50 text-left text-xs font-medium text-muted-foreground">
                {["Strategy", "Symbol", "Entered", "Filled / open", "Entry", "Initial stop", "Stop", "₹ at risk", "Stop at broker", "Stop qty", "Next action", "Findings", "Exit rule"].map(
                  (h) => (
                    <th key={h} scope="col" className="whitespace-nowrap py-2.5 pl-4 pr-3">
                      {h}
                    </th>
                  ),
                )}
              </tr>
            </thead>
            <tbody>
              {data.rows.map((r) => (
                <tr
                  key={`${r.sleeve}-${r.position_id}`}
                  className={`border-b border-border/60 align-top last:border-0 ${resolved(r) ? "" : "bg-negative-muted/40"}`}
                >
                  <td className="py-2.5 pl-4 pr-3">
                    {r.sleeve}
                    {r.simulated ? <Badge className="ml-1">simulated</Badge> : null}
                  </td>
                  <td className="py-2.5 pl-4 pr-3 font-medium">{r.symbol}</td>
                  <td className="whitespace-nowrap py-2.5 pl-4 pr-3">{r.entry_date}</td>
                  <td className="py-2.5 pl-4 pr-3">
                    {r.filled_qty} / {r.open_qty}
                  </td>
                  <td className="py-2.5 pl-4 pr-3">{fmtInr(r.entry_avg, 2)}</td>
                  <td className="py-2.5 pl-4 pr-3">{fmtInr(r.initial_stop, 2)}</td>
                  <td className="py-2.5 pl-4 pr-3">{fmtInr(r.stop, 2)}</td>
                  <td className="py-2.5 pl-4 pr-3">{fmtInr(r.rupee_risk, 2)}</td>
                  <td className="py-2.5 pl-4 pr-3">
                    <b>{r.stop_state}</b>
                    {r.overdue ? (
                      <Badge variant="negative" className="ml-1">
                        overdue
                      </Badge>
                    ) : null}
                    {r.gtt_id ? <div className="text-xs text-muted-foreground">#{r.gtt_id}</div> : null}
                  </td>
                  <td className="py-2.5 pl-4 pr-3">{r.stop_qty ?? "—"}</td>
                  <td className="min-w-[14rem] py-2.5 pl-4 pr-3">{r.next_action}</td>
                  <td className="min-w-[16rem] py-2.5 pl-4 pr-3">
                    {r.issues.map((i) => (
                      <div key={i} className="text-xs text-muted-foreground">
                        {i}
                      </div>
                    ))}
                  </td>
                  <td className="min-w-[18rem] py-2.5 pl-4 pr-3 text-xs text-muted-foreground">{r.exit_rule}</td>
                </tr>
              ))}
              {!data.rows.length ? (
                <tr>
                  <td colSpan={13} className="py-4 pl-4 pr-3 text-muted-foreground">
                    No strategy holds anything. A name in the Kite account that no strategy bought is the weekly rebalancer&apos;s
                    or yours; adopt it below if a strategy should manage it.
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>
      </div>
      <AdoptForm onDone={reload} />
    </div>
  );
}

