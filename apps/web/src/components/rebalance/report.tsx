"use client";

import type { Route } from "next";
import Link from "next/link";

import type { ExecuteResult, ExecutedOrder } from "@/lib/rebalance/desk";
import { inr } from "@/lib/rebalance/desk";
import { cn } from "@/lib/utils";

type Kind = "ok" | "sim" | "info" | "bad";

interface Reading {
  kind: Kind;
  title: string;
  fix?: string;
}

// Anything not named here reads as a failure: a new upstream status must never look like success.
const ORDER_STATUS: Record<string, Reading> = {
  PLACED: { kind: "ok", title: "submitted" },
  DRY_RUN: {
    kind: "sim",
    title: "simulated — not sent",
    fix: "DRY_RUN is on, so nothing reached the exchange.",
  },
  DUPLICATE: {
    kind: "info",
    title: "already sent",
    fix: "This plan had already been executed, so no second order went out.",
  },
  RISK_BLOCKED: {
    kind: "bad",
    title: "refused by risk limits",
    fix: "Nothing was sent. Raise the limit or cut position size, then re-run Analyze.",
  },
  BLOCKED: {
    kind: "bad",
    title: "refused by a guard",
    fix: "Nothing was sent, and a retry will not change that. The reason beside each symbol says which guard.",
  },
  ABORTED: {
    kind: "bad",
    title: "not attempted — batch stopped",
    fix: "Three orders in a row failed with the same message, so the rest were not sent. Fix the cause, then re-run Analyze.",
  },
  REJECTED: {
    kind: "bad",
    title: "refused by the broker",
    fix: "Kite refused this and said why, so it never reached the exchange. Fix that, then re-run Analyze.",
  },
  ERROR: {
    kind: "bad",
    title: "no answer from the broker",
    fix: "The exchange status is UNKNOWN — an order may still have been accepted. Check the Kite order list before re-running Analyze.",
  },
};

const STOP_OK = new Set(["GTT_PLACED", "DRY_RUN_GTT"]);

function read(o: { status?: string }): Reading {
  return (
    ORDER_STATUS[o.status ?? ""] ?? {
      kind: "bad",
      title: `unrecognised status — ${o.status || "none"}`,
      fix: "Treat it as unresolved and check the Kite order list.",
    }
  );
}

const TONE: Record<Kind, string> = {
  ok: "border-positive/40 bg-positive-muted",
  sim: "border-warning/35 bg-warning-muted",
  info: "border-warning/35 bg-warning-muted",
  bad: "border-negative/40 bg-negative-muted",
};

export function ExecutionReport({ result }: { result: ExecuteResult }) {
  const orders = result.orders ?? [];
  const recon = result.reconciliation ?? {};
  const n = { ok: 0, sim: 0, info: 0, bad: 0 };
  for (const o of orders) n[read(o).kind] += 1;
  const head =
    [
      n.ok ? `${n.ok} submitted` : null,
      n.sim ? `${n.sim} simulated` : null,
      n.info ? `${n.info} already sent` : null,
      n.bad ? `${n.bad} failed` : null,
    ]
      .filter(Boolean)
      .join(" · ") || "nothing to place";
  const tone: Kind = n.bad ? "bad" : n.sim || n.info ? "sim" : "ok";
  const bought = orders.filter(
    (o) => read(o).kind === "ok" && (o.action === "BUY" || o.action === "ADD"),
  ).length;

  const groups = new Map<string, { r: Reading; rows: ExecutedOrder[] }>();
  for (const o of orders) {
    const r = read(o);
    if (r.kind === "ok") continue;
    const g = groups.get(r.title) ?? { r, rows: [] };
    g.rows.push(o);
    groups.set(r.title, g);
  }
  for (const g of (result.gtt ?? []).filter((x) => !STOP_OK.has(x.status ?? ""))) {
    const title = "stop not armed";
    const entry = groups.get(title) ?? {
      r: { kind: "bad" as const, title, fix: "The position may be held with no downside protection. Arm it from Stops." },
      rows: [],
    };
    entry.rows.push(g);
    groups.set(title, entry);
  }
  const rank: Record<Kind, number> = { bad: 0, info: 1, sim: 2, ok: 3 };
  const blocks = [...groups.values()].sort((a, b) => rank[a.r.kind] - rank[b.r.kind]);

  const pf = result.preflight;
  const tiles: [string, number | undefined][] = [
    ["Planned sells", recon.planned_sell_value],
    ["Planned buys", recon.planned_buy_value],
    ["Reached the exchange", recon.submitted_value],
    ["Refused", recon.failed_value],
  ];
  if (recon.aborted_value) tiles.push(["Held back", recon.aborted_value]);

  return (
    <section aria-labelledby="exec-report" className="flex flex-col gap-4">
      <h2 id="exec-report" className="text-lg font-semibold">
        Execution report
      </h2>
      <div role="status" className={cn("rounded-xl border p-3.5 text-sm", TONE[tone])}>
        <p className="font-semibold">{head}</p>
        {result.dry_run ? <p className="mt-1">DRY_RUN is on — nothing reached the exchange.</p> : null}
        {n.ok ? (
          <p className="mt-1 text-muted-foreground">
            Submitted is not filled. These are LIMIT orders at the plan&apos;s reference price; any that
            don&apos;t trade stay working until the close and then lapse. Fills appear on{" "}
            <Link className="underline" href="/tradebook">
              Tradebook
            </Link>{" "}
            after tomorrow&apos;s capture.
          </p>
        ) : null}
      </div>

      {pf?.checked ? (
        <p className={cn("rounded-xl border p-3.5 text-sm", TONE[(pf.shortfall ?? 0) > 0 ? "bad" : "ok"])}>
          {(pf.shortfall ?? 0) > 0
            ? `Short ${inr(pf.shortfall)}. The plan needs ${inr(pf.required)} of margin and ${inr(pf.available)} is available; the shortfall lands on the last orders.`
            : `Funded. ${inr(pf.required)} required against ${inr(pf.available)} available.`}
        </p>
      ) : pf?.error ? (
        <p className={cn("rounded-xl border p-3.5 text-sm", TONE.sim)}>
          Funding not checked — the margin query failed ({pf.error}).
        </p>
      ) : null}

      {result.stops_pending === false ? null : (
        <div className={cn("rounded-xl border p-3.5 text-sm", TONE.sim)}>
          <p>
            {bought
              ? `${bought} position${bought === 1 ? "" : "s"} opened or added by this run ${bought === 1 ? "is" : "are"} `
              : "Your holdings are "}
            <b>not yet protected</b>.{" "}
            <Link className="underline" href={"/stops" as Route}>
              Arm stops
            </Link>{" "}
            once the fills settle — every buy gets a GTT stop the same session, sized from what actually filled.
          </p>
          {result.stops_note ? <p className="mt-1 text-xs text-muted-foreground">{result.stops_note}</p> : null}
        </div>
      )}

      {blocks.length ? (
        <div className="flex flex-col gap-3">
          <h3 className="text-sm font-semibold">What needs your attention</h3>
          {blocks.map((g) => (
            <div key={g.r.title} className={cn("rounded-xl border p-3.5 text-sm", TONE[g.r.kind])}>
              <h4 className="font-semibold">
                {g.rows.length} {g.r.title}
              </h4>
              <ul className="mt-2 flex flex-col gap-1">
                {g.rows.map((o, i) => (
                  <li key={`${o.symbol}-${i}`} className="flex flex-wrap justify-between gap-2">
                    <span>
                      {o.action ?? ""} {o.symbol}
                    </span>
                    <span className="text-muted-foreground">{o.error || o.status || ""}</span>
                  </li>
                ))}
              </ul>
              {g.r.fix ? <p className="mt-2 text-xs text-muted-foreground">{g.r.fix}</p> : null}
            </div>
          ))}
        </div>
      ) : null}

      {recon.orders ? (
        <div className="flex flex-col gap-2">
          <h3 className="text-sm font-semibold">Value moved</h3>
          <div className="grid grid-cols-[repeat(auto-fit,minmax(10rem,1fr))] gap-2">
            {tiles.map(([label, value]) => (
              <div key={label} className="rounded-lg border border-border/70 bg-card p-3">
                <div className="text-xs text-muted-foreground">{label}</div>
                <div className="text-base font-semibold tabular-nums">{inr(value ?? 0)}</div>
              </div>
            ))}
          </div>
          {!recon.reached_exchange ? (
            <p className="text-xs text-muted-foreground">
              Nothing reached the exchange. No order can be live, so re-running Analyze after fixing the cause
              cannot double-send.
            </p>
          ) : null}
          {recon.aborted_symbols?.length ? (
            <p className="text-xs text-muted-foreground">
              Held back by the circuit breaker: {recon.aborted_symbols.join(", ")}.
            </p>
          ) : null}
          {recon.not_submitted?.length ? (
            <p className="text-xs text-muted-foreground">Never submitted: {recon.not_submitted.join(", ")}.</p>
          ) : null}
        </div>
      ) : null}

      <details className="text-xs">
        <summary className="cursor-pointer text-muted-foreground">Raw response</summary>
        <pre className="mt-2 max-h-96 overflow-auto rounded-lg bg-muted p-3">{JSON.stringify(result, null, 2)}</pre>
      </details>
    </section>
  );
}
