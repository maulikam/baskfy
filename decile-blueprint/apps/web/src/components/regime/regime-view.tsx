"use client";

import dynamic from "next/dynamic";
import { useEffect, useState, type ReactNode } from "react";

import {
  Card,
  Code,
  DASH,
  DataTable,
  Kv,
  Muted,
  Pill,
  Section,
  TONE,
  Tc,
  Tr,
  f,
  grouped,
  inr,
  signed,
  stateTone,
  tierBg,
  tierText,
} from "@/components/regime/primitives";
import type {
  IndexCard,
  ProposedOrder,
  RegimeData,
  RegimePayload,
  TaxDetail,
} from "@/components/regime/types";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

// Same-origin through Caddy's /desk proxy; GET only — this page never changes state.
export const REGIME_DATA_URL = "/desk/regime/data";

const RegimeTimeline = dynamic(
  () => import("@/components/regime/regime-timeline").then((m) => m.RegimeTimeline),
  { ssr: false, loading: () => <Skeleton className="h-40 w-full" /> },
);

/**
 * The desk's `NewBuyMode` is `full | half | blocked` — never `none`. A map keyed `none` once
 * left a blocked book with no explanation (PC5, docs/pc-findings/pc5.md §3.4). An unknown mode
 * prints as the desk wrote it rather than being mapped to a guess.
 */
const BUY_HEADLINE: Record<string, string> = {
  full: "Full size",
  half: "Half size",
  blocked: "Blocked",
};
const BUY_SENTENCE: Record<string, string> = {
  full: "New positions open at full size.",
  half: "New positions open at half size.",
  blocked: "No new positions are being opened.",
};
const NO_NEW_BUY_POLICY = "The desk did not record a new-buy policy on this evaluation.";

const HEALTH_ROWS: readonly [string, string][] = [
  ["20", "Weight above its 20-DMA"],
  ["50", "Short-term structure"],
  ["200", "Long-term structure, the R3 gate"],
];

const GATE_TONE: Record<string, string> = {
  Triggered: TONE.bad,
  Clear: TONE.ok,
  Met: TONE.ok,
  "Not met": TONE.na,
  "Not evaluable": TONE.warn,
  "Not required": TONE.na,
};

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

type Load =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; payload: RegimePayload };

export function RegimeView() {
  const [load, setLoad] = useState<Load>({ status: "loading" });

  useEffect(() => {
    const ctrl = new AbortController();
    fetch(REGIME_DATA_URL, { credentials: "same-origin", cache: "no-store", signal: ctrl.signal })
      .then(async (res) => {
        if (!res.ok) throw new Error(`The desk answered ${res.status}.`);
        setLoad({ status: "ready", payload: (await res.json()) as RegimePayload });
      })
      .catch((err: unknown) => {
        if (ctrl.signal.aborted) return;
        setLoad({
          status: "error",
          message: err instanceof Error ? err.message : "The desk could not be reached.",
        });
      });
    return () => ctrl.abort();
  }, []);

  if (load.status === "loading") {
    return (
      <div className="flex flex-col gap-3" aria-busy="true">
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }
  if (load.status === "error") {
    return (
      <p role="status" className="rounded-xl border border-warning/35 bg-warning-muted p-3.5 text-sm">
        The desk&apos;s regime data could not be loaded. {load.message}
      </p>
    );
  }
  const v = load.payload;
  if (!v.available) {
    return (
      <Card title="No evaluation yet">
        <p className="text-sm text-muted-foreground">
          {v.message ? `${v.message} ` : ""}This page fills in once a weekly evaluation has been
          stored. Nothing here computes on demand; it reports what was decided and saved.
        </p>
      </Card>
    );
  }
  return <RegimeBody v={v} />;
}

function RegimeBody({ v }: { v: RegimeData }) {
  return (
    <div className="flex flex-col gap-6">
      <StatusStrip v={v} />
      <Banners v={v} />
      <Decide v={v} />
      <section className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.6fr)]">
        <TierCard v={v} />
        <ExposureCard v={v} />
      </section>
      <Why v={v} />
      <Section title="Index health">
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {v.index_cards.map((c) => (
            <IndexHealthCard key={c.index_name} c={c} />
          ))}
        </div>
      </Section>
      <section className="grid gap-3 lg:grid-cols-2">
        <StructuralHealth v={v} />
        <CategoryWeights v={v} />
      </section>
      <section className="grid gap-3 lg:grid-cols-2">
        <Breadth v={v} />
        <VetoCard v={v} />
      </section>
      <CurrentPortfolio v={v} />
      <ProposedActions v={v} />
      <TaxReview v={v} />
      <Execution v={v} />
      <History v={v} />
      <DataQuality v={v} />
      <TierLadder v={v} />
      <ModeExplainer v={v} />
      <Configuration v={v} />
    </div>
  );
}

function StatusStrip({ v }: { v: RegimeData }) {
  const healthTones: Record<string, string> = { Current: TONE.ok, Stale: TONE.warn, Incomplete: TONE.hold };
  const health = healthTones[v.data_health] ?? "";
  const items: [string, string, string][] = [
    ["Rollout mode", (v.mode ?? "observe").replace(/^\w/, (c) => c.toUpperCase()), TONE.hold],
    ["DRY_RUN", v.dry_run ? "On" : "Off", v.dry_run ? TONE.hold : TONE.warn],
    ["Signal date", v.signal_session_date ?? DASH, ""],
    ["Data health", v.data_health, health],
    ["Last evaluation", v.scheduled_week_end ?? DASH, ""],
    ["Next evaluation", v.next_evaluation_date ?? DASH, ""],
    ["Algorithm", v.algorithm_version, "font-mono text-xs"],
    ["Config", v.config_hash.slice(0, 10), "font-mono text-xs"],
  ];
  return (
    <dl className="grid grid-cols-2 gap-px overflow-hidden rounded-xl border border-border/70 bg-border/70 sm:grid-cols-4 xl:grid-cols-8">
      {items.map(([label, value, tone]) => (
        <div key={label} className="bg-card px-3 py-2">
          <dt className="eyebrow">{label}</dt>
          <dd className={cn("mt-0.5 text-sm font-medium tabular-nums", tone)}>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

function Banners({ v }: { v: RegimeData }) {
  if (v.banners.length === 0) return null;
  return (
    <div className="flex flex-col gap-2">
      {v.banners.map((b) => (
        <div
          key={b.title}
          role="status"
          className={cn(
            "flex flex-wrap items-baseline gap-2 rounded-xl border p-3 text-sm",
            b.level === "warn" ? "border-warning/35 bg-warning-muted" : "border-info/35 bg-info-muted",
          )}
        >
          <Pill kind={b.level === "warn" ? "warn" : "hold"}>{b.level === "warn" ? "Warning" : "Hold"}</Pill>
          <b className="font-semibold">{b.title}</b>
          <span className="text-muted-foreground">{b.detail}</span>
        </div>
      ))}
    </div>
  );
}

function Decide({ v }: { v: RegimeData }) {
  const pending = v.proposed_orders.length;
  return (
    <section aria-label="Decision summary" className="grid gap-px overflow-hidden rounded-xl border border-border/70 bg-border/70 sm:grid-cols-2 lg:grid-cols-5">
      <DecideItem q="What regime">
        <b className={cn("text-lg font-semibold", tierText(v.tier_key))} data-testid="regime-tier">
          {v.policy_tier} {v.tier_name}
        </b>
        <Muted>{v.tier_meaning}</Muted>
      </DecideItem>
      <DecideItem q="Why">
        <b className="text-sm font-semibold">{v.headline_reason}</b>
        <a className="text-xs text-muted-foreground underline underline-offset-2" href="#why">
          every gate below
        </a>
      </DecideItem>
      <DecideItem q="Exposure permitted">
        <b className="text-lg font-semibold tabular-nums">{f(v.target_equity_cap_pct, 0)}%</b>
        <Muted>
          holding {f(v.actual_equity_pct)}% across {v.holdings_now ?? DASH} positions
          {v.required_reduction_pct ? (
            <>
              {" · "}
              <b className={TONE.bad}>{f(v.required_reduction_pct)}% to sell</b>
            </>
          ) : (
            " · within cap"
          )}
        </Muted>
      </DecideItem>
      <DecideItem q="Pending changes">
        <b className={cn("text-lg font-semibold", pending ? TONE.bad : "")}>{pending || "None"}</b>
        <Muted>{pending ? "orders proposed" : (v.no_plan_reason ?? "no rebalance planned")}</Muted>
      </DecideItem>
      <DecideItem q="Data reliable">
        <b className={cn("text-lg font-semibold", v.data_health === "Current" ? TONE.ok : TONE.bad)}>
          {v.data_health}
        </b>
        <Muted>
          signal {v.signal_session_date ?? DASH} · next {v.next_evaluation_date ?? DASH}
          {v.breadth_coverage_pct !== null ? ` · breadth ${f(v.breadth_coverage_pct, 0)}% covered` : ""}
        </Muted>
      </DecideItem>
    </section>
  );
}

function DecideItem({ q, children }: { q: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1 bg-card p-4">
      <span className="eyebrow">{q}</span>
      {children}
    </div>
  );
}

function TierCard({ v }: { v: RegimeData }) {
  return (
    <Card title="Policy tier">
      <div className={cn("text-4xl font-semibold leading-none", tierText(v.tier_key))}>{v.policy_tier}</div>
      <div className={cn("mt-1 text-lg font-medium", tierText(v.tier_key))}>{v.tier_name}</div>
      <Muted className="mt-1.5">{v.tier_meaning}</Muted>
      <div className="mt-3">
        <Kv label="Raw candidate">
          {v.raw_candidate_tier} · {v.raw_candidate_name}
        </Kv>
        <Kv label="Previous tier">{v.previous_policy_tier ?? "none"}</Kv>
        <Kv label="Transition limiting" tone={v.transition_limited ? TONE.warn : TONE.na}>
          {v.transition_limited ? "Applied" : "Not applied"}
        </Kv>
        <Kv label="Last transition">{v.last_transition_date ?? "none"}</Kv>
        <Kv label="Recovery override" tone={v.override_active ? TONE.hold : TONE.na}>
          {v.override_active ? "Active" : "Inactive"}
        </Kv>
        <Kv label="Momentum-50 veto" tone={v.sentinel_veto ? TONE.bad : TONE.ok}>
          {v.sentinel_veto ? "Active" : "Inactive"}
        </Kv>
      </div>
    </Card>
  );
}

function newBuyTone(mode: string | null): string {
  if (mode === "blocked") return TONE.bad;
  if (mode === "full") return TONE.ok;
  return TONE.warn;
}

function ExposureCard({ v }: { v: RegimeData }) {
  const bar = v.bar;
  const capAt = bar.cap_marker ?? 0;
  const largest = v.largest_position?.symbol
    ? `${v.largest_position.symbol} · ${f(v.largest_position.weight_sleeve)}%`
    : DASH;
  const clusterMax = Object.values(v.cluster_weights);
  return (
    <Card title="Equity exposure">
      <p className="text-sm leading-relaxed">
        Target equity <b>≤{f(v.target_equity_cap_pct ?? 0)}%</b> · Actual equity <b>{f(v.actual_equity_pct)}%</b>
        {v.required_reduction_pct && v.required_reduction_pct > 0 ? (
          <>
            {" · "}
            <b className={TONE.bad}>Reduction pending {f(v.required_reduction_pct)}%</b>
          </>
        ) : v.actual_equity_pct !== null ? (
          <>
            {" · "}
            <b className={TONE.ok}>Within cap</b>
          </>
        ) : null}
        <br />
        New purchases{" "}
        <b className={newBuyTone(v.new_buys)} data-testid="new-buy-headline">
          {v.new_buys ? (BUY_HEADLINE[v.new_buys] ?? v.new_buys) : "Not recorded"}
        </b>
        {v.forced_action !== "none" ? (
          <>
            {" · "}Forced action <b className={TONE.warn}>{v.forced_action_label.toLowerCase()}</b>
          </>
        ) : null}
      </p>
      <p className="mt-1 text-xs text-muted-foreground" data-testid="new-buy-explanation">
        {v.new_buys
          ? (BUY_SENTENCE[v.new_buys] ??
            `The desk recorded its new-buy policy as "${v.new_buys}", which this page does not have a sentence for.`)
          : NO_NEW_BUY_POLICY}
      </p>

      {bar.within_cap !== null ? (
        <>
          <div className="relative mt-4 pb-5">
            <div
              role="img"
              aria-label={`Equity ${f(v.actual_equity_pct)} percent against a cap of ${f(v.target_equity_cap_pct)} percent`}
              className="flex h-7 overflow-hidden rounded-md bg-muted text-[11px] font-medium text-white"
            >
              {bar.within_cap > 0 ? (
                <div className={cn("flex items-center px-2", tierBg(v.tier_key))} style={{ width: `${bar.within_cap}%` }}>
                  {bar.within_cap > 13 ? `${f(bar.within_cap)}% within cap` : ""}
                </div>
              ) : null}
              {bar.excess && bar.excess > 0 ? (
                <div
                  className="flex items-center bg-negative px-2 [background-image:repeating-linear-gradient(45deg,transparent_0_4px,rgb(255_255_255/0.15)_4px_8px)]"
                  style={{ width: `${bar.excess}%` }}
                >
                  {bar.excess > 10 ? `${f(bar.excess)}% over` : ""}
                </div>
              ) : null}
              {bar.cash && bar.cash > 0 ? (
                <div className="flex items-center px-2 text-muted-foreground" style={{ width: `${bar.cash}%` }}>
                  {bar.cash > 10 ? `${f(bar.cash)}% cash` : ""}
                </div>
              ) : null}
            </div>
            <div className="absolute top-[-3px] h-[34px] w-0.5 bg-foreground" style={{ left: `${capAt}%` }} />
            <div
              className="absolute top-8 whitespace-nowrap text-[10.5px] text-muted-foreground"
              style={{
                left: `${capAt}%`,
                transform: `translateX(${capAt >= 97 ? "-100%" : capAt <= 4 ? "0" : "-50%"})`,
              }}
            >
              cap {f(capAt, 0)}%
            </div>
          </div>
          <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
            <LegendSwatch className={tierBg(v.tier_key)}>Equity within cap</LegendSwatch>
            <LegendSwatch className="bg-negative">Over cap · reduction required</LegendSwatch>
            <LegendSwatch className="bg-muted ring-1 ring-border">Cash</LegendSwatch>
          </div>
        </>
      ) : (
        <Muted className="mt-3">
          No exposure observation recorded for this evaluation, so the cap cannot be compared against reality yet.
        </Muted>
      )}

      <div className="mt-3">
        <DataTable label="Equity exposure">
          <Tr>
            <Tc tone={TONE.na}>Actual equity</Tc>
            <Tc>{f(v.actual_equity_pct)}%</Tc>
            <Tc tone={TONE.na}>Holdings</Tc>
            <Tc>{v.holdings_now ?? DASH}</Tc>
          </Tr>
          <Tr>
            <Tc tone={TONE.na}>Permitted equity</Tc>
            <Tc>{f(v.target_equity_cap_pct)}%</Tc>
            <Tc tone={TONE.na}>Target range</Tc>
            <Tc>
              {v.target_position_range[0]}–{v.target_position_range[1]}
            </Tc>
          </Tr>
          <Tr>
            <Tc tone={TONE.na}>Required reduction</Tc>
            <Tc tone={v.required_reduction_pct ? TONE.bad : TONE.na}>{f(v.required_reduction_pct)}%</Tc>
            <Tc tone={TONE.na}>At full weight</Tc>
            <Tc>
              {v.full_sized ?? DASH}
              {v.half_sized.length || v.below_half.length ? (
                <span className="text-muted-foreground">
                  {" "}
                  · {v.half_sized.length} half · {v.below_half.length} thinner
                </span>
              ) : null}
            </Tc>
          </Tr>
          <Tr>
            <Tc tone={TONE.na}>Current cash</Tc>
            <Tc>{f(v.current_cash_pct)}%</Tc>
            <Tc tone={TONE.na}>Largest position</Tc>
            <Tc>{largest}</Tc>
          </Tr>
          <Tr>
            <Tc tone={TONE.na}>Pending sells</Tc>
            <Tc>{f(v.pending_sell_pct)}%</Tc>
            <Tc tone={TONE.na}>Cluster cap usage</Tc>
            <Tc>{clusterMax.length ? `${f(Math.max(...clusterMax))}% of ${f(v.cluster_cap, 0)}%` : DASH}</Tc>
          </Tr>
          <Tr>
            <Tc tone={TONE.na}>Unresolved gap</Tc>
            <Tc tone={v.exposure_gap_pct && v.exposure_gap_pct > 0 ? TONE.bad : TONE.ok}>
              {signed(v.exposure_gap_pct)}%
            </Tc>
            <Tc tone={TONE.na}>Allocation</Tc>
            <Tc
              tone={v.allocation_feasible ? TONE.ok : v.allocation_feasible === false ? TONE.bad : TONE.na}
            >
              {v.allocation_feasible ? "Feasible" : v.allocation_feasible === false ? "Infeasible" : DASH}
            </Tc>
          </Tr>
        </DataTable>
      </div>
      {v.exposure_cap_mismatch ? (
        <Muted className="mt-2">
          <Pill kind="warn">note</Pill> The stored reconciliation row carries an older cap; the cap shown is derived
          from tier {v.policy_tier}.
        </Muted>
      ) : null}
    </Card>
  );
}

function LegendSwatch({ className, children }: { className: string; children: ReactNode }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <i aria-hidden className={cn("inline-block size-2.5 rounded-sm", className)} />
      {children}
    </span>
  );
}

function Why({ v }: { v: RegimeData }) {
  return (
    <Section title="Why this regime" id="why">
      <Card>
        <ul className="flex list-disc flex-col gap-1 pl-5 text-sm">
          {v.narrative.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
        <div className="mt-4">
          <DataTable label="Why this regime" head={["Gate", "Current", "Threshold", "Status", "Confirmation"]}>
            {v.gates.map((g) => (
              <Tr key={g.code}>
                <Tc>
                  {g.label}
                  <br />
                  <Code>{g.code}</Code>
                </Tc>
                <Tc className="whitespace-nowrap">{g.current ?? DASH}</Tc>
                <Tc tone={TONE.na} className="whitespace-nowrap">
                  {g.threshold}
                </Tc>
                <Tc tone={GATE_TONE[g.status] ?? TONE.na} className="whitespace-nowrap font-medium">
                  {g.status}
                </Tc>
                <Tc tone={TONE.na}>{g.note}</Tc>
              </Tr>
            ))}
          </DataTable>
        </div>
        <Muted className="mt-3">
          Every gate is listed, not only those that fired, because a tier is auditable only if you can see what did
          not trigger.
        </Muted>
      </Card>
    </Section>
  );
}

function IndexHealthCard({ c }: { c: IndexCard }) {
  const s = c.spark;
  return (
    <Card>
      <div className="flex items-baseline justify-between gap-2">
        <b className="text-sm font-semibold">{c.index_name}</b>
        {c.is_sentinel ? <Pill kind="hold">sentinel</Pill> : null}
      </div>
      <div className="mb-0.5 mt-1.5 text-2xl font-semibold tabular-nums">{grouped(c.close, 1)}</div>
      <Muted>
        close · {c.data_date ?? DASH}{" "}
        {c.stale ? <Pill kind="warn">stale</Pill> : <Pill kind="mute">current</Pill>}
      </Muted>
      {s ? (
        <>
          <svg
            viewBox={`0 0 ${s.w} ${s.h}`}
            className="mb-0.5 mt-2 block h-auto w-full"
            style={{ maxWidth: s.w * 1.6 }}
            aria-hidden="true"
            focusable="false"
          >
            <path d={s.ma} fill="none" className="stroke-muted-foreground" strokeWidth={1} strokeDasharray="3 2" />
            <path
              d={s.price}
              fill="none"
              strokeWidth={1.5}
              strokeLinejoin="round"
              className={s.above ? "stroke-positive" : "stroke-negative"}
            />
            <circle cx={s.last_x} cy={s.last_y} r={2.2} className={s.above ? "fill-positive" : "fill-negative"} />
          </svg>
          <Muted className="text-[10.5px]">
            {s.sessions} sessions · dashed line is the {s.ma_len}-DMA
          </Muted>
        </>
      ) : null}
      <div className="mt-2">
        <DataTable label="Index health">
          {c.mas.map((m) => (
            <Tr key={m.length}>
              <Tc>{m.length}-DMA</Tc>
              <Tc tone={TONE.na}>{m.ma ? grouped(m.ma) : DASH}</Tc>
              <Tc tone={stateTone(m.state)} className="whitespace-nowrap">
                <span title={m.state}>{signed(m.distance_pct, 2)}%</span>
              </Tc>
              <Tc tone={TONE.na} className="whitespace-nowrap">
                {m.confirmation}
              </Tc>
            </Tr>
          ))}
        </DataTable>
      </div>
      <Muted className="mt-2">
        Bearish MA stack: <b className={c.bearish_stack ? TONE.bad : TONE.na}>{c.bearish_stack ? "Yes" : "No"}</b>
      </Muted>
    </Card>
  );
}

function StructuralHealth({ v }: { v: RegimeData }) {
  const stack = v.bearish_stack_weight;
  return (
    <Card title="Weighted structural health">
      <DataTable label="Weighted structural health" head={["Measure", "Value", "Interpretation"]}>
        {HEALTH_ROWS.map(([k, note]) => (
          <Tr key={k}>
            <Tc>H{k}</Tc>
            <Tc>{f(v.health[k], 2)}</Tc>
            <Tc tone={TONE.na}>{note}</Tc>
          </Tr>
        ))}
        <Tr>
          <Tc>Bearish stack</Tc>
          <Tc tone={stack !== null && stack >= 0.6 ? TONE.bad : ""}>{f(stack, 2)}</Tc>
          <Tc tone={TONE.na}>Weight in full bearish alignment</Tc>
        </Tr>
      </DataTable>
    </Card>
  );
}

function CategoryWeights({ v }: { v: RegimeData }) {
  const rows = Object.entries(v.book_weights).sort(([a], [b]) => a.localeCompare(b));
  return (
    <Card title="Strategic category weights">
      <DataTable label="Strategic category weights" head={["Category", "Weight", "Source"]}>
        {rows.map(([cat, w]) => (
          <Tr key={cat}>
            <Tc>{cat}</Tc>
            <Tc>{f(w * 100, 0)}%</Tc>
            <Tc tone={TONE.na}>{v.book_weight_source.replaceAll("_", " ")}</Tc>
          </Tr>
        ))}
      </DataTable>
      {v.book_weights_are_defaults ? (
        <Muted className="mt-3">
          <Pill kind="warn">fallback</Pill> Configuration defaults are in use: no full-risk target portfolio or committed R1
          snapshot was available. These describe the portfolio the strategy wants at full risk, never the residual already
          reduced by the regime.
        </Muted>
      ) : null}
    </Card>
  );
}

function Gauge({ v, dim }: { v: RegimeData; dim?: boolean }) {
  return (
    <div
      className={cn("relative mb-3 mt-2 flex h-5 overflow-visible rounded-md text-[10px] text-white", dim && "opacity-35")}
      {...(dim
        ? { "aria-hidden": true }
        : { role: "img", "aria-label": `Breadth ${f(v.breadth_pct, 0)} percent` })}
    >
      {v.breadth_bands.map((b, i) => (
        <div
          key={b.key}
          className={cn(
            "flex items-center px-1",
            tierBg(b.key),
            i === 0 && "rounded-l-md",
            i === v.breadth_bands.length - 1 && "rounded-r-md",
          )}
          style={{ width: `${Math.min(b.hi, 100) - b.lo}%` }}
        >
          {Math.trunc(b.lo)}
        </div>
      ))}
      {!dim && v.breadth_pct !== null ? (
        <div
          className="absolute -top-1 h-7 w-1 -translate-x-1/2 rounded-sm bg-foreground ring-2 ring-card"
          style={{ left: `${v.breadth_pct}%` }}
        />
      ) : null}
    </div>
  );
}

function Breadth({ v }: { v: RegimeData }) {
  const recOk =
    v.recovery_confirmations !== null &&
    v.recovery_required !== null &&
    v.recovery_required > 0 &&
    v.recovery_confirmations >= v.recovery_required;
  return (
    <Card title="Breadth">
      {v.breadth_pct !== null ? (
        <>
          <div className="text-3xl font-semibold tabular-nums">
            {f(v.breadth_pct, 0)}%{" "}
            <span className={cn("text-base", tierText(v.breadth_band_key))}>{v.breadth_band}</span>
          </div>
          <Gauge v={v} />
          <Kv label="Observed / eligible">
            {v.breadth_observed ?? DASH} of {v.breadth_eligible ?? DASH}
          </Kv>
          <Kv label="Coverage" tone={v.breadth_usable ? TONE.ok : TONE.bad}>
            {f(v.breadth_coverage_pct)}% · need {f(v.min_breadth_coverage_pct, 0)}%
          </Kv>
          <Kv label="Audit date">{v.breadth_as_of ?? DASH}</Kv>
          <Kv label="Universe">{v.breadth_universe ?? DASH}</Kv>
          <Kv label="Recovery confirmations" tone={recOk ? TONE.ok : TONE.na}>
            {v.recovery_confirmations ?? DASH} of {v.recovery_required || DASH}
          </Kv>
          <Kv label="Re-risk needs" tone={TONE.na}>
            breadth ≥ {f(v.config.recovery_breadth_min, 0)}% and H20 ≥ {f(v.config.recovery_h20_min, 2)}
          </Kv>
          <Kv label="Universe hash">
            <Code>{v.breadth_universe_hash ?? DASH}</Code>
          </Kv>
          {!v.breadth_usable ? (
            <Muted className="mt-3">
              <Pill kind="warn">unusable</Pill> Coverage is below requirement, so breadth cannot trigger forced
              selling and cannot permit Risk-On.
            </Muted>
          ) : null}
        </>
      ) : (
        <>
          <Muted>
            No breadth reading is attached to this evaluation. Without one, breadth can neither force selling nor
            permit Risk-On. The tier is decided by structure and the momentum sentinel alone.
          </Muted>
          <Gauge v={v} dim />
          <Muted>Bands: below 30 crash · 30–40 risk-off · 40–55 caution · above 55 recovery.</Muted>
        </>
      )}
    </Card>
  );
}

function VetoCard({ v }: { v: RegimeData }) {
  const veto = v.veto;
  return (
    <Card title="Momentum 50 veto">
      <div className={cn("text-xl font-semibold", veto.active ? TONE.bad : TONE.ok)}>
        {veto.active ? "New-buy veto active" : "No veto active"}
      </div>
      <p className="mb-2 mt-1 text-sm">
        {veto.active ? (
          <>
            The sentinel is <b>{f(veto.dist_50 === null ? null : Math.abs(veto.dist_50))}% below its 50-DMA</b> and
            has confirmed below ({veto.conf_50}).
          </>
        ) : (
          <>
            The sentinel sits <b>{signed(veto.dist_50)}%</b> against its 50-DMA ({veto.conf_50}).
          </>
        )}
      </p>
      <Kv label="vs 50-DMA" tone={stateTone(veto.state_50)}>
        {signed(veto.dist_50, 2)}% · {veto.conf_50}
      </Kv>
      <Kv label="vs 200-DMA" tone={stateTone(veto.state_200)}>
        {signed(veto.dist_200, 2)}% · {veto.conf_200}
      </Kv>
      <Kv label="Effect on policy">{veto.effect}</Kv>
      <Kv label="Release requires">
        <span className="whitespace-normal">{veto.release}</span>
      </Kv>
      <Muted className="mt-3">
        A 50-DMA break vetoes new entries and floors the tier at R2. It never triggers a full liquidation on its own.
      </Muted>
    </Card>
  );
}

function CurrentPortfolio({ v }: { v: RegimeData }) {
  const p = v.portfolio;
  if (!p.available) return null;
  const tradeable = p.tradeable ?? [];
  const maxW = p.max_weight ?? 0;
  const maxPnl = p.max_abs_pnl ?? 0;
  return (
    <Section title={`Current portfolio — ${p.as_of ?? DASH}`}>
      <div className="grid gap-3 lg:grid-cols-2">
        <Card title="Position weights">
          <div className="flex flex-col gap-1.5">
            {tradeable.map((row) => (
              <div key={row.symbol} className="grid grid-cols-[7rem_1fr_4rem] items-center gap-2 text-xs">
                <span className="truncate font-medium">{row.symbol}</span>
                <span
                  className="h-3 overflow-hidden rounded-sm bg-muted"
                  title={`${row.symbol}: ${f(row.weight_pct, 2)}% of NAV, ₹${grouped(row.value)}`}
                >
                  <span
                    className="block h-full bg-accent"
                    style={{ width: `${maxW ? ((row.weight_pct ?? 0) / maxW) * 100 : 0}%` }}
                  />
                </span>
                <span className="text-right tabular-nums">{f(row.weight_pct, 2)}%</span>
              </div>
            ))}
          </div>
          <div className="mt-1 flex justify-between pl-[7.5rem] pr-[4.5rem] text-[10.5px] text-muted-foreground">
            <span>0%</span>
            <span>{f(maxW)}% of NAV</span>
          </div>
          <Muted className="mt-2">
            Share of total NAV. Cash is {f(p.cash_pct)}% and is not shown as a bar.
          </Muted>
        </Card>
        <Card title="Unrealised profit and loss">
          <div className="flex flex-col gap-1.5">
            {tradeable.map((row) => {
              const pnl = row.pnl_pct ?? 0;
              const width = maxPnl ? (Math.abs(pnl) / maxPnl) * 50 : 0;
              return (
                <div key={row.symbol} className="grid grid-cols-[7rem_1fr_4rem] items-center gap-2 text-xs">
                  <span className="truncate font-medium">{row.symbol}</span>
                  <span
                    className="relative h-3 rounded-sm bg-muted"
                    title={`${row.symbol}: ${signed(row.pnl_pct)}%, ₹${grouped(row.pnl_value)}`}
                  >
                    <span className="absolute inset-y-0 left-1/2 w-px bg-border" />
                    <span
                      className={cn("absolute inset-y-0", pnl >= 0 ? "left-1/2 bg-positive" : "right-1/2 bg-negative")}
                      style={{ width: `${width}%` }}
                    />
                  </span>
                  <span className={cn("text-right tabular-nums", pnl >= 0 ? TONE.ok : TONE.bad)}>{signed(pnl)}%</span>
                </div>
              );
            })}
          </div>
          <div className="mt-1 flex justify-between pl-[7.5rem] pr-[4.5rem] text-[10.5px] text-muted-foreground">
            <span>−{f(maxPnl, 0)}%</span>
            <span>0</span>
            <span>+{f(maxPnl, 0)}%</span>
          </div>
          <Muted className="mt-2">
            {p.winners ?? 0} winners, {p.losers ?? 0} losers. Net unrealised{" "}
            <b className={(p.total_pnl ?? 0) >= 0 ? TONE.ok : TONE.bad}>₹{grouped(p.total_pnl)}</b>. Against cost
            basis, not against the regime cap.
          </Muted>
        </Card>
      </div>
      <Card title="Holdings">
        <DataTable
          label="Holdings"
          head={["Stock", "Qty", "Pledged", "Avg cost ₹", "Price ₹", "Value ₹", "Wt% NAV", "Unrealised", "P&L ₹", "Status"]}
        >
          {(p.positions ?? []).map((row) => (
            <Tr key={row.symbol}>
              <Tc className="font-medium">{row.symbol}</Tc>
              <Tc>{grouped(row.quantity)}</Tc>
              <Tc tone={row.pledged_qty ? TONE.warn : TONE.na}>{row.pledged_qty ? grouped(row.pledged_qty) : DASH}</Tc>
              <Tc>{grouped(row.average_price, 1)}</Tc>
              <Tc>{grouped(row.price, 1)}</Tc>
              <Tc>{grouped(row.value)}</Tc>
              <Tc>{f(row.weight_pct, 2)}</Tc>
              <Tc tone={(row.pnl_pct ?? 0) >= 0 ? TONE.ok : TONE.bad}>{signed(row.pnl_pct)}%</Tc>
              <Tc tone={(row.pnl_value ?? 0) >= 0 ? TONE.ok : TONE.bad}>{grouped(row.pnl_value)}</Tc>
              <Tc tone={TONE.na}>{row.excluded ? <Pill kind="warn">untouchable</Pill> : "strategy"}</Tc>
            </Tr>
          ))}
        </DataTable>
        <Muted className="mt-3">
          {p.count ?? 0} strategy positions worth ₹{grouped(p.invested)}, cash ₹{grouped(p.cash)}, NAV ₹
          {grouped(p.nav)}.
          {p.excluded_count
            ? ` ${p.excluded_count} untouchable position worth ₹${grouped(p.excluded_value)} is held but sits outside the strategy, so it is excluded from NAV-based exposure.`
            : ""}
          {p.pledged_count
            ? ` ${p.pledged_count} positions carry pledged quantity; those shares sell directly on Zerodha.`
            : ""}
        </Muted>
      </Card>
    </Section>
  );
}

function orderStatusPill(status: string): "ok" | "warn" | "hold" {
  if (status === "FILLED") return "ok";
  if (status === "FAILED" || status === "BLOCKED") return "warn";
  return "hold";
}

function ProposedActions({ v }: { v: RegimeData }) {
  const rows: ProposedOrder[] = [
    ...v.guard_orders,
    ...v.proposed_sells.filter((o) => o.note !== "guard exit"),
    ...v.proposed_buys,
  ];
  const ps = v.plan_summary;
  return (
    <Section title="Proposed portfolio actions">
      <Card>
        {v.proposed_orders.length ? (
          <>
            <Muted className="mb-2">
              {v.proposed_orders.length} orders · {v.proposed_sells.length} sell, {v.proposed_buys.length} buy
              {ps.tier !== undefined
                ? ` · tier ${ps.tier}, allocation ${ps.sleeve_pct ?? DASH}%, projected ${f(ps.projected_equity_pct)}% equity`
                : ""}
              . Read-only: nothing executes from this page.
            </Muted>
            <DataTable
              label="Proposed portfolio actions"
              head={["Stock", "Action", "Now", "Δ qty", "Final", "Wt now", "Wt target", "Value ₹", "Unrealised", "To LTCG", "Rank", "Reason", "Status"]}
            >
              {rows.map((o) => {
                const sell = o.delta < 0;
                return (
                  <Tr key={`${o.symbol}-${o.action}`}>
                    <Tc className="font-medium">{o.symbol}</Tc>
                    <Tc tone={sell ? TONE.bad : TONE.ok}>{o.action}</Tc>
                    <Tc>{o.qty_now}</Tc>
                    <Tc tone={sell ? TONE.bad : TONE.ok}>{`${o.delta >= 0 ? "+" : ""}${o.delta}`}</Tc>
                    <Tc>{o.qty_final}</Tc>
                    <Tc>{o.weight_now !== null ? `${f(o.weight_now, 2)}%` : DASH}</Tc>
                    <Tc>{o.weight_target !== null ? `${f(o.weight_target, 2)}%` : DASH}</Tc>
                    <Tc className="whitespace-nowrap">
                      {grouped(o.value)} {o.liquidity_warning ? <Pill kind="warn">thin</Pill> : null}
                    </Tc>
                    <Tc tone={(o.pnl_pct ?? 0) >= 0 ? TONE.ok : TONE.bad}>
                      {o.pnl_pct !== null ? `${signed(o.pnl_pct)}%` : DASH}
                    </Tc>
                    <Tc tone={o.days_to_ltcg && o.days_to_ltcg <= 45 ? TONE.warn : TONE.na}>
                      {o.days_to_ltcg ? `${o.days_to_ltcg}d` : sell && o.tax_flagged !== null ? "long term" : DASH}
                    </Tc>
                    <Tc>{o.rank ?? DASH}</Tc>
                    <Tc tone={TONE.na} className="whitespace-nowrap">
                      {o.note === "guard exit" ? (
                        <Pill kind="warn">guard exit</Pill>
                      ) : sell ? (
                        "regime exposure cut"
                      ) : (
                        "regime target"
                      )}
                      {o.half_sized ? (
                        <>
                          {" "}
                          <Pill kind="hold">half</Pill>
                        </>
                      ) : null}
                      {o.tax_review ? (
                        <>
                          {" "}
                          <Pill kind="warn">tax review</Pill>
                        </>
                      ) : null}
                    </Tc>
                    <Tc tone={TONE.na} className="whitespace-nowrap">
                      {o.order_status ? (
                        <>
                          <Pill kind={orderStatusPill(o.order_status)}>{o.order_status.toLowerCase()}</Pill>
                          {o.filled_qty ? ` ${o.filled_qty} filled` : ""}
                        </>
                      ) : o.tax_flagged ? (
                        "Manual"
                      ) : (
                        "Planned"
                      )}
                    </Tc>
                  </Tr>
                );
              })}
            </DataTable>
            <Muted className="mt-3">
              Guard-driven exits are listed first and labelled; they take precedence over every regime action. Weights
              are shown against both total NAV and the active equity sleeve.
            </Muted>
          </>
        ) : v.no_plan_reason ? (
          <>
            <p className="text-sm text-muted-foreground">{v.no_plan_reason}</p>
            <Muted className="mt-2">
              Upload a weekly scan on the desk to produce a ranked rebalance. Regime-driven selling appears here
              automatically whenever actual exposure exceeds the tier cap.
            </Muted>
          </>
        ) : (
          <p className="text-sm text-muted-foreground">No portfolio changes proposed for this evaluation.</p>
        )}
      </Card>
    </Section>
  );
}

function TaxRow({ m }: { m: TaxDetail }) {
  const soon = m.lots.map((l) => l.days_to_ltcg).filter((d): d is number => d !== null && d > 0);
  const soonest = soon.length ? Math.min(...soon) : null;
  return (
    <>
      <Tr>
        <Tc className="font-semibold">{m.symbol}</Tc>
        <Tc>{m.requested_qty}</Tc>
        <Tc tone={m.uncovered_qty ? TONE.bad : TONE.ok}>
          {m.covered_qty} {m.uncovered_qty ? <Pill kind="warn">{m.uncovered_qty} with no basis</Pill> : null}
        </Tc>
        <Tc>
          {m.short_term_qty} / {m.long_term_qty}
        </Tc>
        <Tc tone={(m.estimated_gain ?? 0) >= 0 ? TONE.ok : TONE.bad}>
          {m.estimated_gain !== null ? grouped(m.estimated_gain) : "unknown"}
        </Tc>
        <Tc tone={soonest !== null && soonest <= 45 ? TONE.warn : TONE.na}>
          {soonest !== null ? `${soonest}d` : "all long term"}
        </Tc>
        <Tc tone={TONE.na}>
          <span className="flex flex-wrap gap-1">
            {m.manual_review ? <Pill kind="warn">manual</Pill> : <Pill kind="ok">clear</Pill>}
            {m.codes.map((c) => (
              <Pill key={c} kind="hold">
                {c}
              </Pill>
            ))}
          </span>
        </Tc>
      </Tr>
      <tr>
        <td colSpan={7} className="pb-3">
          <details>
            <summary className="cursor-pointer text-xs text-muted-foreground">
              {m.lots.length} FIFO lot{m.lots.length === 1 ? "" : "s"} consumed by this sale
            </summary>
            <div className="mt-1.5">
              <DataTable
                label={`FIFO lots for ${m.symbol}`}
                head={["Acquired", "Qty", "Cost ₹", "Held", "To LTCG", "Gain ₹", "Treatment"]}
              >
                {m.lots.map((l, i) => (
                  <Tr key={`${l.acquired_on ?? "unknown"}-${i}`}>
                    <Tc>{l.acquired_on ?? "unknown"}</Tc>
                    <Tc>{l.quantity}</Tc>
                    <Tc>{l.acquired_at !== null ? grouped(l.acquired_at, 1) : "unknown"}</Tc>
                    <Tc tone={TONE.na}>{l.days_held !== null ? `${l.days_held}d` : DASH}</Tc>
                    <Tc tone={l.days_to_ltcg && l.days_to_ltcg <= 45 ? TONE.warn : TONE.na}>
                      {l.days_to_ltcg ? `${l.days_to_ltcg}d` : DASH}
                    </Tc>
                    <Tc tone={(l.gain ?? 0) >= 0 ? TONE.ok : TONE.bad}>
                      {l.gain !== null ? grouped(l.gain) : "unknown"}
                    </Tc>
                    <Tc tone={l.long_term ? TONE.ok : TONE.na}>
                      {l.long_term ? "long term" : l.long_term === false ? "short term" : "unknown"}
                    </Tc>
                  </Tr>
                ))}
              </DataTable>
            </div>
          </details>
        </td>
      </tr>
    </>
  );
}

function TaxReview({ v }: { v: RegimeData }) {
  return (
    <Section title="Tax and manual review">
      <Card>
        <Muted className="mb-2.5">
          Every proposed sale, with the FIFO lots it would consume. Shown whether or not anything is flagged: deciding to
          approve a sale needs the lots and what each realises, and detail that appears only when something is already
          wrong is missing in exactly the case where the sale is fine but large.
        </Muted>
        <DataTable
          label="Tax review of proposed sales"
          head={["Position", "Sell qty", "Covered", "Short / long", "Est. gain ₹", "Soonest LTCG", "Review"]}
        >
          {v.tax_detail.length ? (
            v.tax_detail.map((m) => <TaxRow key={m.symbol} m={m} />)
          ) : (
            <Tr>
              <Tc colSpan={7} tone={TONE.na}>
                No sales are proposed, so there is nothing to review. Lots are re-read from the tradebook each time a
                plan is built.
              </Tc>
            </Tr>
          )}
        </DataTable>
        <Muted className="mt-3">
          A tax flag is a review warning, not a recommendation to hold. It defers a sale only when an alternative exists,
          and never leaves the exposure cap silently breached. A bonus share carries <b>nil cost</b> and its holding
          period starts at allotment, so it shows as short term for a year after it lands.
        </Muted>
      </Card>
    </Section>
  );
}

function Execution({ v }: { v: RegimeData }) {
  const rp = v.rebalance_plan;
  const gapOpen = v.exposure_gap_pct !== null && v.exposure_gap_pct > 0;
  const money = (x: number | null | undefined) => (x ? inr(x) : DASH);
  return (
    <Section title="Execution and reconciliation">
      <Card>
        <p className="text-sm">
          Policy requires <b>≤{f(v.target_equity_cap_pct)}%</b> equity.{" "}
          {v.actual_equity_pct !== null ? (
            <>
              Current exposure <b>{f(v.actual_equity_pct)}%</b>.{" "}
              {gapOpen ? (
                <b className={TONE.bad}>{f(v.exposure_gap_pct)}% reduction still pending.</b>
              ) : (
                <b className={TONE.ok}>Cap satisfied.</b>
              )}
            </>
          ) : (
            "No exposure has been observed for this evaluation yet."
          )}
        </p>
        <div className="mt-2">
          <DataTable label="Execution and reconciliation">
            <Tr>
              <Tc tone={TONE.na}>Evaluation ID</Tc>
              <Tc>
                <Code>{v.evaluation_id}</Code>
              </Tc>
              <Tc tone={TONE.na}>Execution status</Tc>
              <Tc>{v.execution_status.toUpperCase()}</Tc>
            </Tr>
            <Tr>
              <Tc tone={TONE.na}>Rebalance plan</Tc>
              <Tc>
                {rp ? (
                  <>
                    <Code>{rp.plan_id}</Code> {rp.linked ? null : <Pill kind="mute">not linked</Pill>}
                  </>
                ) : (
                  <span className="text-muted-foreground">none stored</span>
                )}
              </Tc>
              <Tc tone={TONE.na}>Orders in plan</Tc>
              <Tc>{rp ? rp.orders : DASH}</Tc>
            </Tr>
            <Tr>
              <Tc tone={TONE.na}>Planned sell value</Tc>
              <Tc>{money(v.planned_sell_value)}</Tc>
              <Tc tone={TONE.na}>Submitted</Tc>
              <Tc>{money(rp?.submitted_value)}</Tc>
            </Tr>
            <Tr>
              <Tc tone={TONE.na}>Planned buy value</Tc>
              <Tc>{money(v.planned_buy_value)}</Tc>
              <Tc tone={TONE.na}>Filled</Tc>
              <Tc tone={rp?.filled_value ? TONE.ok : TONE.na}>{money(rp?.filled_value)}</Tc>
            </Tr>
            <Tr>
              <Tc tone={TONE.na}>Order states</Tc>
              <Tc tone={TONE.na}>
                {rp && Object.keys(rp.counts).length
                  ? Object.entries(rp.counts)
                      .map(([k, n]) => `${k.toLowerCase()} ${n}`)
                      .join(", ")
                  : DASH}
              </Tc>
              <Tc tone={TONE.na}>Failed / rejected</Tc>
              <Tc tone={rp?.failed_value ? TONE.bad : TONE.na}>{money(rp?.failed_value)}</Tc>
            </Tr>
            <Tr>
              <Tc tone={TONE.na}>Remaining gap</Tc>
              <Tc tone={gapOpen ? TONE.bad : TONE.ok}>{signed(v.exposure_gap_pct)}%</Tc>
              <Tc tone={TONE.na}>Last reconciliation</Tc>
              <Tc>{v.exposure_observed_at ?? DASH}</Tc>
            </Tr>
          </DataTable>
        </div>
        <Muted className="mt-3">
          A tier on this screen never guarantees the portfolio reached its cap. Filled is what actually moved: under DRY_RUN
          every order reaches <Code>dry_run</Code> and nothing fills, which is why submitted can carry a value while
          filled stays blank.
          {rp && !rp.linked ? " The plan shown is the most recent one stored, not one built under this evaluation." : ""}
        </Muted>
      </Card>
    </Section>
  );
}

const HISTORY_TONE: Record<string, string> = {
  R1: tierText("on"),
  R2: tierText("caution"),
  R3: tierText("off"),
  R4: tierText("crash"),
};

function History({ v }: { v: RegimeData }) {
  const tl = v.timeline;
  const n = v.history.length;
  return (
    <Section title="Transition history">
      <Card>
        {tl ? (
          <>
            <Muted className="mb-1.5">
              {tl.first} to {tl.last}. Cap, actual exposure and breadth are all percentages, so they share one axis. The
              cap is a step: it changes only at a transition and holds between evaluations.
            </Muted>
            <div className="mb-2 flex flex-wrap gap-4 text-xs text-muted-foreground">
              <LegendSwatch className="bg-warning">permitted cap</LegendSwatch>
              <LegendSwatch className="bg-positive">actual equity</LegendSwatch>
              <LegendSwatch className="bg-info">breadth</LegendSwatch>
            </div>
            <RegimeTimeline history={v.history} />
            <Muted className="mt-1.5">{tl.omitted}, so it is not drawn rather than approximated.</Muted>
          </>
        ) : n ? (
          <Muted className="mb-2.5">
            A timeline needs at least {v.timeline_min_weeks} distinct weeks; there {n === 1 ? "is" : "are"} {n}. Two
            points make a line that looks like a trend and is not one, so the table carries the numbers until then.
          </Muted>
        ) : null}
        {n > 1 ? (
          <div className="mt-3">
            <DataTable
              label="Transition history"
              head={["Week", "Raw candidate", "Policy tier", "Cap", "Actual", "Trigger", "Record"]}
            >
              {v.history.map((h) => (
                <Tr key={h.week}>
                  <Tc>{h.week}</Tc>
                  <Tc>{h.raw}</Tc>
                  <Tc tone={HISTORY_TONE[h.tier] ?? ""} className="font-medium">
                    {h.tier}
                  </Tc>
                  <Tc>{f(h.cap, 0)}%</Tc>
                  <Tc>{f(h.actual)}%</Tc>
                  <Tc tone={TONE.na}>
                    <Code>{h.trigger}</Code> {h.transition_limited ? <Pill kind="hold">capped</Pill> : null}
                  </Tc>
                  <Tc tone={TONE.na}>{h.committed ? "committed" : "preview"}</Tc>
                </Tr>
              ))}
            </DataTable>
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">
            Only one evaluation has been recorded, so there is no transition history yet. The timeline gains a row per
            weekly evaluation.
          </p>
        )}
      </Card>
    </Section>
  );
}

function DataQuality({ v }: { v: RegimeData }) {
  const gaps = v.index_status.reduce((sum, r) => sum + r.gap_count, 0);
  const st = v.store_status;
  return (
    <Section title="Data quality">
      <Card>
        {v.safe_state ? (
          <p className="mb-3 text-sm">
            <Pill kind="hold">safe state</Pill> {v.safe_state}
          </p>
        ) : null}
        <div className="grid gap-4 lg:grid-cols-2">
          <DataTable label="Data quality" head={["Index", "Last final session", "Sessions", "Age", "State"]}>
            {v.index_status.map((r) => (
              <Tr key={r.index_name}>
                <Tc>{r.index_name}</Tc>
                <Tc>{r.last_final_session ?? "no data"}</Tc>
                <Tc>{grouped(r.sessions)}</Tc>
                <Tc>{r.age_days ?? DASH}d</Tc>
                <Tc tone={r.stale ? TONE.bad : TONE.ok}>{r.stale ? "Stale" : "Current"}</Tc>
              </Tr>
            ))}
          </DataTable>
          <DataTable label="Data quality">
            <Tr>
              <Tc tone={TONE.na}>Breadth freshness</Tc>
              <Tc>{v.breadth_as_of ?? "none"}</Tc>
            </Tr>
            <Tr>
              <Tc tone={TONE.na}>Breadth coverage</Tc>
              <Tc tone={v.breadth_usable ? TONE.ok : TONE.bad}>
                {v.breadth_coverage_pct !== null ? f(v.breadth_coverage_pct) : "none"}
              </Tc>
            </Tr>
            <Tr>
              <Tc tone={TONE.na}>Classification coverage</Tc>
              <Tc tone={v.book_weights_are_defaults ? TONE.bad : TONE.ok}>
                {v.book_weights_are_defaults ? "defaults in use" : "sufficient"}
              </Tc>
            </Tr>
            <Tr>
              <Tc tone={TONE.na}>Tax-lot completeness</Tc>
              <Tc tone={v.tax_data_incomplete ? TONE.bad : TONE.ok}>
                {v.tax_data_incomplete ? "incomplete" : "complete for proposed sales"}
              </Tc>
            </Tr>
            <Tr>
              <Tc tone={TONE.na}>Possible gaps in index data</Tc>
              <Tc tone={gaps ? TONE.warn : TONE.ok}>
                {gaps} run{gaps === 1 ? "" : "s"} over 5 days
                {gaps ? (
                  <span className="text-muted-foreground">
                    {" "}
                    · holidays are not known here, so these are possible rather than confirmed
                  </span>
                ) : null}
              </Tc>
            </Tr>
            <Tr>
              <Tc tone={TONE.na}>Forced actions permitted</Tc>
              <Tc tone={v.forced_actions_permitted ? TONE.ok : TONE.warn}>
                {v.forced_actions_permitted ? "yes" : "no"}
              </Tc>
            </Tr>
            {st ? (
              <>
                <Tr>
                  <Tc tone={TONE.na}>Last collection</Tc>
                  <Tc tone={st.collection_healthy ? TONE.ok : TONE.bad}>
                    {st.last_collection ?? "never"}
                    {st.last_collection_outcome ? ` · ${st.last_collection_outcome}` : ""}
                    {st.consecutive_failures ? ` · ${st.consecutive_failures} consecutive failures` : ""}
                  </Tc>
                </Tr>
                <Tr>
                  <Tc tone={TONE.na}>Last index write</Tc>
                  <Tc>{st.last_index_write ?? "never"}</Tc>
                </Tr>
                <Tr>
                  <Tc tone={TONE.na}>Database</Tc>
                  <Tc>
                    schema v{st.schema_version} · {st.journal_mode} · {f((st.db_bytes ?? 0) / 1048576)} MB
                  </Tc>
                </Tr>
              </>
            ) : null}
          </DataTable>
        </div>
      </Card>
    </Section>
  );
}

function TierLadder({ v }: { v: RegimeData }) {
  return (
    <Section title="What the four tiers mean">
      <div className="flex flex-col gap-2">
        {v.tier_ladder.map((t) => (
          <div
            key={t.tier}
            className={cn(
              "grid grid-cols-[6.5rem_1fr] gap-4 rounded-xl border bg-card p-4",
              t.current ? "border-foreground/40 ring-1 ring-foreground/10" : "border-border/70",
            )}
          >
            <div>
              <div className={cn("text-xl font-semibold", tierText(t.key))}>{t.tier}</div>
              <div className="text-xs text-muted-foreground">equity ≤{f(t.cap, 0)}%</div>
              <div className="text-xs text-muted-foreground">buys: {t.buys.toLowerCase()}</div>
            </div>
            <div className="text-sm">
              <h3 className={cn("flex items-center gap-2 font-semibold", tierText(t.key))}>
                {t.name}
                {t.current ? <Pill kind="mute">current</Pill> : null}
              </h3>
              <p className="mt-1 text-muted-foreground">
                <b className="text-foreground">Entered when:</b> {t.when}
              </p>
              <p className="mt-0.5 text-muted-foreground">
                <b className="text-foreground">What it does:</b> {t.does}
              </p>
            </div>
          </div>
        ))}
      </div>
      <Muted>
        Risk reduction is immediate: R1 can drop straight to R3 or R4. Re-risking climbs one rung per weekly evaluation,
        so R4 back to R1 takes three weeks.
      </Muted>
    </Section>
  );
}

function ModeExplainer({ v }: { v: RegimeData }) {
  return (
    <Section title="How the overlay is running">
      <div className="grid gap-3 md:grid-cols-3">
        {v.mode_explainer.map((sw) => (
          <Card key={sw.label}>
            <div className="eyebrow">{sw.label}</div>
            <div className={cn("mt-1 text-lg font-semibold", sw.state === "on" ? TONE.ok : TONE.hold)}>{sw.value}</div>
            <Muted className="mt-1">{sw.means}</Muted>
          </Card>
        ))}
      </div>
      <Muted>
        These three are independent. Dry run is the global guarantee: it holds in every mode, so an order cannot be
        placed even in enforce.
      </Muted>
    </Section>
  );
}

function Configuration({ v }: { v: RegimeData }) {
  const c = v.config;
  const kv = (rows: [string, ReactNode][]) => (
    <DataTable label="How the overlay is running">
      {rows.map(([k, val]) => (
        <Tr key={k}>
          <Tc tone={TONE.na}>{k}</Tc>
          <Tc>{val}</Tc>
        </Tr>
      ))}
    </DataTable>
  );
  const sorted = (o: Record<string, number>) => Object.entries(o).sort(([a], [b]) => a.localeCompare(b));
  return (
    <details className="rounded-xl border border-border/70 bg-card p-4">
      <summary className="cursor-pointer text-sm font-semibold">Configuration (read-only)</summary>
      <div className="mt-3 grid gap-4 lg:grid-cols-2">
        {kv([
          ["MA lengths", c.ma_lengths.join(" · ")],
          ["Buffer", `${c.buffer_bps} bps · ${f(c.buffer_bps / 100, 2)}%`],
          ["Confirmation days", c.confirm_days],
          ["Weekly evaluation", WEEKDAYS[c.weekly_evaluation_weekday] ?? DASH],
          ["Breadth required", c.breadth_required ? "yes" : "no"],
          ["Min breadth coverage", `${f(c.min_breadth_coverage_pct, 0)}%`],
          ["Index stale after", `${c.index_stale_days}d`],
          ["LTCG review window", `${c.ltcg_review_days}d`],
        ])}
        {kv([
          ...Object.entries(c.breadth_thresholds).map(([k, val]): [string, string] => [`Breadth · ${k}`, val]),
          ...Object.entries(c.health_thresholds).map(([k, val]): [string, string] => [`Health · ${k}`, val]),
        ])}
        {kv([
          ...sorted(c.exposure_map).map(([k, val]): [string, string] => [`Exposure · ${k}`, `${f(val, 0)}%`]),
          ...sorted(c.default_weights).map(([k, val]): [string, string] => [
            `Default weight · ${k}`,
            `${f(val * 100, 0)}%`,
          ]),
        ])}
        {kv([
          ["R4 residual policy", c.r4_residual],
          ["Whipsaw definition", c.whipsaw],
          ["Algorithm version", <Code key="a">{c.algorithm_version}</Code>],
          ["Config hash", <Code key="h">{c.config_hash}</Code>],
        ])}
      </div>
      <Muted className="mt-3">
        Configuration cannot be edited here. Change it in <Code>app/config.py</Code> or the environment; the hash changes
        with it, which is what keeps a historical decision reproducible.
      </Muted>
    </details>
  );
}
