"use client";

import type { Route } from "next";
import Link from "next/link";
import { useState } from "react";

import { Row, Stat, StatRow, Table, Td, Th } from "@/components/desk/ui";
import { Banner, Card, ConfirmAction, Field, Muted, useDeskPage } from "@/components/ops/shared";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { useIsStaff } from "@/components/shell/staff-context";
import { errorText, postDesk, postDeskFile } from "@/lib/desk-ops/client";

/** `GET /desk/tradebook` (Accept: application/json): the desk page's `{s, f, actions, have_snapshot}`. */
interface LotDetail {
  symbol: string;
  holding_qty: number;
  lot_qty: number;
  lots: number;
  first_acquired: string | null;
  avg_cost: number | null;
  broker_avg: number;
  status: string;
}

interface LotStatus {
  has_lots: boolean;
  open_lots: number;
  closed_trades: number;
  realised_pnl: number | null;
  history_from: string | null;
  coverage: { holdings: number; with_lots: number; coverage_pct: number };
  problems: { symbol: string; holding_qty: number; lot_qty: number; difference: number; likely_cause: string }[];
  detail: LotDetail[];
}

interface Fills {
  total: number;
  sources: { source: string; label: string; count: number; first: string; last: string; last_run: string | null }[];
  captured_today: boolean;
  capture_ever_run: boolean;
}

interface CorporateAction {
  id: number;
  symbol: string;
  ex_date: string;
  describe: string;
  note: string | null;
}

interface TradebookView {
  s: LotStatus;
  f: Fills | null;
  actions: CorporateAction[];
  have_snapshot: boolean;
}

const EMPTY_ACTION = { symbol: "", kind: "bonus", ex_date: "", ratio_new: "1", ratio_old: "1", note: "" };

export function TaxLots() {
  const staff = useIsStaff();
  const { data, error, reload } = useDeskPage<TradebookView>("/tradebook");
  const [notice, setNotice] = useState<{ ok: boolean; text: string } | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [action, setAction] = useState(EMPTY_ACTION);

  if (!staff) return null;
  if (error && !data) return <Banner tone="bad" tag="tax lots unavailable">{error}</Banner>;
  if (!data) return <Muted>Loading tax lots…</Muted>;

  const done = async (fn: () => Promise<{ message: string }>) => {
    try {
      const res = await fn();
      setNotice({ ok: true, text: res.message });
    } catch (e) {
      setNotice({ ok: false, text: `${errorText(e)} Nothing was written.` });
    }
    await reload();
  };

  const { s, f } = data;
  const cov = s.coverage.coverage_pct;
  const actionValid = action.symbol.trim() !== "" && /^\d{4}-\d{2}-\d{2}$/.test(action.ex_date.trim());
  const setA = (k: keyof typeof EMPTY_ACTION, v: string) => setAction((a) => ({ ...a, [k]: v }));

  return (
    <div className="flex flex-col gap-5">
      <h2 className="text-lg font-semibold">Tax lots</h2>
      {notice ? (
        <Banner tone={notice.ok ? "ok" : "bad"} tag={notice.ok ? "recorded" : "rejected"}>
          {notice.text}
        </Banner>
      ) : null}

      <StatRow>
        <Stat
          label="Lot coverage"
          value={`${cov.toFixed(0)}%`}
          tone={cov >= 99 ? "text-positive" : cov > 0 ? "text-warning" : "text-negative"}
          hint={`${s.coverage.with_lots} of ${s.coverage.holdings} holdings have acquisition history`}
        />
        <Stat label="Open lots" value={String(s.open_lots)} {...(s.history_from ? { hint: `oldest acquired ${s.history_from}` } : {})} />
        <Stat
          label="Closed trades"
          value={s.closed_trades.toLocaleString("en-IN")}
          {...(s.realised_pnl !== null
            ? { hint: `realised ₹${Math.round(s.realised_pnl).toLocaleString("en-IN")}` }
            : {})}
        />
        <Stat
          label="Lot mismatches"
          value={s.has_lots ? String(s.problems.length) : "n/a"}
          tone={!s.has_lots ? "" : s.problems.length ? "text-negative" : "text-positive"}
          hint={s.has_lots ? "quantity mismatches against real holdings" : "nothing to reconcile until lots are imported"}
        />
      </StatRow>

      {!data.have_snapshot ? (
        <Banner tone="warn" tag="no snapshot">
          Nothing to reconcile against yet — record an end-of-day snapshot first.
        </Banner>
      ) : null}

      <Card title="Import a Console tradebook">
        <Muted className="mb-2">
          Kite Connect exposes only today&apos;s trades, so acquisition dates for anything bought earlier come from an
          export (console.zerodha.com → Reports → Tradebook, a range reaching back before your oldest open position).
          Re-importing the same file changes nothing.
        </Muted>
        <div className="flex flex-wrap items-center gap-3">
          <input
            type="file"
            accept=".csv"
            aria-label="Tradebook CSV"
            className="text-sm"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          />
          <ConfirmAction
            label="Import tradebook"
            confirmLabel="Import"
            variant="primary"
            disabled={!file}
            warning={<>Import <b>{file?.name}</b> and rebuild the lots it covers.</>}
            onConfirm={() => done(() => postDeskFile("/tradebook", "file", file as File))}
          />
        </div>
      </Card>

      <Card title="Corporate actions">
        <Muted className="mb-2">
          A bonus or split changes what you hold with no transaction, so lots rebuilt from fills alone come out short.
          A <b>bonus</b> share has nil cost and its holding period starts at allotment; a <b>split</b> spreads cost per
          share and inherits the original holding period. Ratio is <b>new:old</b> — a 1:1 bonus is 1 and 1, a share
          splitting into two is 2 and 1. Confirm the ex-date from Console.
        </Muted>
        <div className="grid gap-3 sm:grid-cols-3 lg:grid-cols-6">
          <Field id="ca-stock" label="Stock">
            <Input id="ca-stock" value={action.symbol} placeholder="ANANDRATHI" onChange={(e) => setA("symbol", e.target.value.toUpperCase())} />
          </Field>
          <Field id="ca-type" label="Type">
            <Select id="ca-type" value={action.kind} onChange={(e) => setA("kind", e.target.value)}>
              <option value="bonus">Bonus</option>
              <option value="split">Split</option>
            </Select>
          </Field>
          <Field id="ca-ex-date" label="Ex-date">
            <Input id="ca-ex-date" value={action.ex_date} placeholder="YYYY-MM-DD" onChange={(e) => setA("ex_date", e.target.value)} />
          </Field>
          <Field id="ca-ratio-new" label="Ratio new">
            <Input id="ca-ratio-new" value={action.ratio_new} onChange={(e) => setA("ratio_new", e.target.value)} />
          </Field>
          <Field id="ca-ratio-old" label="Ratio old">
            <Input id="ca-ratio-old" value={action.ratio_old} onChange={(e) => setA("ratio_old", e.target.value)} />
          </Field>
          <Field id="ca-note" label="Note">
            <Input id="ca-note" value={action.note} placeholder="optional" onChange={(e) => setA("note", e.target.value)} />
          </Field>
        </div>
        <div className="mt-3">
          <ConfirmAction
            label="Record and rebuild"
            confirmLabel="Record it"
            variant="primary"
            disabled={!actionValid}
            warning={
              <>
                Record a <b>{action.ratio_new}:{action.ratio_old} {action.kind}</b> in <b>{action.symbol}</b> ex{" "}
                {action.ex_date} and rebuild its lots.
              </>
            }
            onConfirm={() =>
              done(async () => {
                const res = await postDesk("/tradebook/corporate-action", action);
                setAction(EMPTY_ACTION);
                return res;
              })
            }
          />
        </div>
        {data.actions.length ? (
          <div className="mt-4">
            <Table
              caption="Corporate actions"
              head={
                <>
                  <Th>Stock</Th>
                  <Th>Ex-date</Th>
                  <Th>Effect</Th>
                  <Th>Remove</Th>
                </>
              }
            >
              {data.actions.map((a) => (
                <Row key={a.id}>
                  <Td>
                    <b>{a.symbol}</b>
                  </Td>
                  <Td>{a.ex_date}</Td>
                  <Td>
                    <span className="text-xs text-muted-foreground">
                      {a.describe}
                      {a.note ? ` · ${a.note}` : ""}
                    </span>
                  </Td>
                  <td className="py-2 pl-4 pr-3">
                    <ConfirmAction
                      label="Remove"
                      confirmLabel="Remove"
                      variant="destructive"
                      warning={`Remove this action and rebuild ${a.symbol}'s lots.`}
                      onConfirm={() =>
                        done(() =>
                          postDesk("/tradebook/corporate-action/delete", { action_id: String(a.id), symbol: a.symbol }),
                        )
                      }
                    />
                  </td>
                </Row>
              ))}
            </Table>
            <Muted className="mt-2">Removing one rebuilds that symbol&apos;s lots too: nothing adjusted is stored.</Muted>
          </div>
        ) : null}
      </Card>

      <Card title="Where the lots come from">
        {f && f.total ? (
          <Table
            caption="Where the lots come from"
            head={
              <>
                <Th>Source</Th>
                <Th align="right">Fills</Th>
                <Th>Covers</Th>
                <Th>Last recorded</Th>
              </>
            }
          >
            {f.sources.map((src) => (
              <Row key={src.source}>
                <Td>{src.label}</Td>
                <Td align="right">{src.count.toLocaleString("en-IN")}</Td>
                <Td>
                  {src.first} → {src.last}
                </Td>
                <Td>{src.last_run ?? "—"}</Td>
              </Row>
            ))}
          </Table>
        ) : (
          <Muted>No fills stored yet. Import an export above to seed the history.</Muted>
        )}
        <div className="mt-3">
          {f?.captured_today ? (
            <Banner tone="ok" tag="captured">
              Today&apos;s session has been recorded.
            </Banner>
          ) : (
            <Banner tone="warn" tag={f?.capture_ever_run ? "not yet today" : "capture idle"}>
              {f?.capture_ever_run
                ? "Today's fills are not recorded yet. Kite's trade list is same-day only; the daily run captures it, or run it now from "
                : "Automatic capture has never run. Every session you trade needs a run that day from "}
              <Link className="text-accent underline-offset-4 hover:underline" href={"/ops" as Route}>
                Operations
              </Link>
              .
            </Banner>
          )}
        </div>
      </Card>

      {s.has_lots ? (
        <section className="flex flex-col gap-2">
          <h3 className="text-base font-semibold">Holdings against their lots</h3>
          <Table
            caption="Holdings against their lots"
            head={
              <>
                <Th>Stock</Th>
                <Th align="right">Holding qty</Th>
                <Th align="right">Lot qty</Th>
                <Th align="right">Lots</Th>
                <Th>First acquired</Th>
                <Th align="right">Lot avg ₹</Th>
                <Th align="right">Broker avg ₹</Th>
                <Th>Status</Th>
              </>
            }
          >
            {s.detail.map((d) => (
              <Row key={d.symbol}>
                <Td>{d.symbol}</Td>
                <Td align="right">{d.holding_qty.toLocaleString("en-IN")}</Td>
                <Td align="right" tone={d.status !== "ok" ? "text-negative" : ""}>
                  {d.lot_qty.toLocaleString("en-IN")}
                </Td>
                <Td align="right">{d.lots || "—"}</Td>
                <Td>{d.first_acquired ?? "—"}</Td>
                <Td align="right">{d.avg_cost !== null ? d.avg_cost.toLocaleString("en-IN", { maximumFractionDigits: 1 }) : "—"}</Td>
                <Td align="right">{d.broker_avg.toLocaleString("en-IN", { maximumFractionDigits: 1 })}</Td>
                <Td>
                  {d.status === "ok" ? (
                    <Badge variant="positive">reconciled</Badge>
                  ) : d.status === "mismatch" ? (
                    <Badge variant="warning">mismatch</Badge>
                  ) : (
                    <Badge variant="warning">no history</Badge>
                  )}
                </Td>
              </Row>
            ))}
          </Table>
        </section>
      ) : (
        <Banner tone="warn" tag="no lots imported">
          Every tax review returns TAX_DATA_UNKNOWN and needs manual sign-off until a tradebook is imported.
        </Banner>
      )}

      {s.has_lots && s.problems.length ? (
        <section className="flex flex-col gap-2">
          <h3 className="text-base font-semibold">Quantity mismatches</h3>
          <Table
            caption="Quantity mismatches"
            head={
              <>
                <Th>Stock</Th>
                <Th align="right">Holding</Th>
                <Th align="right">Lots</Th>
                <Th align="right">Difference</Th>
                <Th>Likely cause</Th>
              </>
            }
          >
            {s.problems.map((p) => (
              <Row key={p.symbol}>
                <Td>{p.symbol}</Td>
                <Td align="right">{p.holding_qty.toLocaleString("en-IN")}</Td>
                <Td align="right">{p.lot_qty.toLocaleString("en-IN")}</Td>
                <Td align="right" tone="text-negative">
                  {p.difference > 0 ? `+${p.difference}` : p.difference}
                </Td>
                <Td>{p.likely_cause}</Td>
              </Row>
            ))}
          </Table>
        </section>
      ) : null}
    </div>
  );
}
