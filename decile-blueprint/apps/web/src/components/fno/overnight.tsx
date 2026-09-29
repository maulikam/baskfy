import { ToneBadge } from "@/components/options/state-chip";
import { formatTradeDate } from "@/lib/format";
import type {
  FnoEvidence,
  FnoF2,
  FnoJournalRow,
  FnoPosition,
  FnoScan,
  FnoUnderlying,
} from "@/lib/fno/types";
import {
  SLEEVE_NAME,
  asOfClose,
  detailStr,
  markedAtSettle,
  percent,
  price,
  ratio,
  rupees,
  signed,
  stateText,
  volPct,
} from "@/lib/fno/view";

import {
  DeskOnlyPrices,
  F2AgainstResearchBanner,
  Tier2ECaveat,
} from "./caveats";
import { UnderlyingLevel } from "./underlying-level";

/**
 * `/options/overnight`'s panels (`docs/fno/05` §2): one card per F1 underlying, the open
 * structures, the journal, the evidence card, and the F2 section under its permanent banner.
 * Read-only: nothing here is a button, and every number is the last close or its settle.
 */

export interface Leg {
  role: string;
  strike: string;
  option_type: string;
  settle?: string | null;
}

export function legsOf(detail: Record<string, unknown>): Leg[] {
  const raw = detail.legs;
  if (!Array.isArray(raw)) return [];
  return raw.filter(
    (leg): leg is Leg =>
      typeof leg === "object" &&
      leg !== null &&
      typeof (leg as Leg).role === "string" &&
      typeof (leg as Leg).strike === "string",
  );
}

function strikeOf(legs: Leg[], role: string): number | null {
  const leg = legs.find((l) => l.role === role);
  const n = leg ? Number(leg.strike) : Number.NaN;
  return Number.isFinite(n) ? n : null;
}

/** The two wing widths in points, from the four strikes. */
export function widths(legs: Leg[]): {
  put: number | null;
  call: number | null;
} {
  const sp = strikeOf(legs, "SHORT_PUT");
  const lp = strikeOf(legs, "LONG_PUT");
  const sc = strikeOf(legs, "SHORT_CALL");
  const lc = strikeOf(legs, "LONG_CALL");
  return {
    put: sp !== null && lp !== null ? sp - lp : null,
    call: sc !== null && lc !== null ? lc - sc : null,
  };
}

export function nested(
  detail: Record<string, unknown>,
  key: string,
  field: string,
): string | null {
  const inner = detail[key];
  if (typeof inner !== "object" || inner === null) return null;
  return detailStr(inner as Record<string, unknown>, field);
}

export function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="tabular-nums">{value}</dd>
    </div>
  );
}

export function Reasons({ reasons }: { reasons: string[] }) {
  if (reasons.length === 0) return null;
  return (
    <ul className="list-disc space-y-0.5 pl-5 text-xs text-muted-foreground">
      {reasons.map((reason) => (
        <li key={reason}>{reason}</li>
      ))}
    </ul>
  );
}

function ProposedCondor({ scan }: { scan: FnoScan }) {
  const legs = legsOf(scan.detail);
  if (legs.length === 0) return null;
  const w = widths(legs);
  const ORDER = ["LONG_PUT", "SHORT_PUT", "SHORT_CALL", "LONG_CALL"];
  const sorted = [...legs].sort(
    (a, b) => ORDER.indexOf(a.role) - ORDER.indexOf(b.role),
  );
  return (
    <div className="space-y-2" data-testid="fno-proposed-condor">
      <h4 className="text-sm font-medium">Proposed condor</h4>
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-xs text-muted-foreground">
            <th className="font-normal">Leg</th>
            <th className="font-normal">Strike</th>
            <th className="font-normal">Settle</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((leg) => (
            <tr key={leg.role}>
              <td>{leg.role.replace("_", " ").toLowerCase()}</td>
              <td className="tabular-nums">
                {price(leg.strike)} {leg.option_type}
              </td>
              <td className="tabular-nums">{price(leg.settle ?? null)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm sm:grid-cols-3">
        <Fact label="ATM IV" value={volPct(scan.iv)} />
        <Fact
          label="Credit (points)"
          value={price(detailStr(scan.detail, "credit_points"))}
        />
        <Fact
          label="Widths (put / call)"
          value={`${w.put ?? "—"} / ${w.call ?? "—"}`}
        />
        <Fact
          label="Max loss per lot"
          value={rupees(detailStr(scan.detail, "max_loss_per_lot_inr"))}
        />
        <Fact
          label="Cost share"
          value={percent(nested(scan.detail, "cost", "cost_share_pct"))}
        />
        <Fact
          label="IV ÷ RV20"
          value={`${ratio(scan.iv_rv ?? detailStr(scan.detail, "iv_rv"))} · recorded, not used`}
        />
      </dl>
      {nested(scan.detail, "sizing", "message") ? (
        <p className="text-xs text-muted-foreground">
          {nested(scan.detail, "sizing", "message")}
        </p>
      ) : null}
    </div>
  );
}

export function F1Card({
  underlying,
  scanDate,
}: {
  underlying: FnoUnderlying;
  scanDate: string | null;
}) {
  const { scan } = underlying;
  const state = scan ? stateText(scan.state) : null;
  return (
    <article
      className="space-y-3 rounded-lg border border-border/70 bg-card p-4"
      data-testid={`fno-f1-${underlying.symbol}`}
    >
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <h3 className="text-base font-semibold">{underlying.symbol}</h3>
          <p className="text-xs text-muted-foreground">
            Next F1 entry:{" "}
            <span className="text-foreground" data-testid="fno-next-entry">
              {underlying.next_entry_date
                ? formatTradeDate(underlying.next_entry_date)
                : "not known yet"}
            </span>
          </p>
        </div>
        <UnderlyingLevel
          symbol={underlying.symbol}
          liveSymbol={underlying.level.live_symbol}
          level={underlying.level.level}
          closeOf={underlying.level.close_of}
        />
      </header>
      {scan && state ? (
        <>
          <div className="flex flex-wrap items-center gap-2">
            <ToneBadge
              text={state.label}
              tone={state.tone}
              testId="fno-state"
            />
            <span className="text-xs text-muted-foreground">
              {asOfClose(scanDate)}
            </span>
          </div>
          <Reasons reasons={scan.reasons} />
          <ProposedCondor scan={scan} />
        </>
      ) : (
        <p
          className="text-sm text-muted-foreground"
          data-testid="fno-unscanned"
        >
          Not scanned yet.
        </p>
      )}
    </article>
  );
}

export function PositionLegs({ legs }: { legs: Record<string, unknown> }) {
  const list = legsOf(legs);
  if (list.length === 0) return null;
  return (
    <p className="text-xs text-muted-foreground">
      {list
        .map(
          (leg) => `${leg.role.replace("_", " ").toLowerCase()} ${leg.strike}`,
        )
        .join(" · ")}
    </p>
  );
}

export function OpenStructures({ positions }: { positions: FnoPosition[] }) {
  return (
    <section className="space-y-3" aria-labelledby="fno-open-heading">
      <h2
        id="fno-open-heading"
        className="text-sm font-medium uppercase tracking-wide text-muted-foreground"
      >
        Open structures
      </h2>
      {positions.length === 0 ? (
        <p className="text-sm text-muted-foreground" data-testid="fno-no-open">
          No F1 structure is open.
        </p>
      ) : (
        <ul className="grid gap-3 lg:grid-cols-2">
          {positions.map((p) => (
            <li
              key={p.id}
              className="space-y-2 rounded-lg border border-border/70 bg-card p-4"
              data-testid="fno-open-structure"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-medium">
                  {SLEEVE_NAME[p.sleeve] ?? p.sleeve} ·{" "}
                  {p.simulated ? "paper" : "live"}
                </span>
                <span
                  className="text-xs text-muted-foreground"
                  data-testid="fno-mark-clock"
                >
                  {markedAtSettle(p.mark?.trade_date ?? null)}
                </span>
              </div>
              <PositionLegs legs={p.legs} />
              <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm sm:grid-cols-3">
                <Fact label="Entry credit" value={price(p.entry_credit)} />
                <Fact
                  label="Mark (settle)"
                  value={price(p.mark?.mark_points ?? null)}
                />
                <Fact label="P&L" value={rupees(p.mark?.pnl_inr ?? null)} />
                <Fact label="P&L in R" value={signed(p.mark?.pnl_r ?? null)} />
                <Fact
                  label="Profit-take level"
                  value={price(p.profit_take_points)}
                />
                <Fact
                  label="Hard exit"
                  value={formatTradeDate(p.hard_exit_date)}
                />
                <Fact
                  label="Days held"
                  value={
                    p.sessions_held === null ? "—" : String(p.sessions_held)
                  }
                />
              </dl>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/** Closed trades in R, one table per (sleeve, paper/live) — never pooled (`03` §6). */
export function JournalTables({
  rows,
  title,
}: {
  rows: FnoJournalRow[];
  title: string;
}) {
  const groups = new Map<string, FnoJournalRow[]>();
  for (const row of rows) {
    const key = `${row.sleeve} · ${row.simulated ? "paper" : "live"}`;
    groups.set(key, [...(groups.get(key) ?? []), row]);
  }
  return (
    <section className="space-y-3" aria-label={title}>
      <h2 className="text-sm font-medium uppercase tracking-wide text-muted-foreground">
        {title}
      </h2>
      {groups.size === 0 ? (
        <p
          className="text-sm text-muted-foreground"
          data-testid="fno-journal-empty"
        >
          Nothing has closed yet.
        </p>
      ) : (
        [...groups.entries()].map(([key, list]) => (
          <div
            key={key}
            className="overflow-x-auto"
            data-testid="fno-journal-group"
          >
            <p className="text-xs font-medium">{key}</p>
            <table className="w-full min-w-[28rem] text-sm">
              <thead>
                <tr className="text-left text-xs text-muted-foreground">
                  <th className="font-normal">Symbol</th>
                  <th className="font-normal">Opened</th>
                  <th className="font-normal">Closed</th>
                  <th className="font-normal">Why</th>
                  <th className="font-normal">Rolls</th>
                  <th className="text-right font-normal">R</th>
                  <th className="text-right font-normal">Net ₹</th>
                </tr>
              </thead>
              <tbody>
                {list.map((row) => (
                  <tr key={row.position_id}>
                    <td>{row.symbol}</td>
                    <td className="tabular-nums">
                      {formatTradeDate(row.opened_on)}
                    </td>
                    <td className="tabular-nums">
                      {formatTradeDate(row.closed_on)}
                    </td>
                    <td>
                      {row.closed_reason.toLowerCase().replaceAll("_", " ")}
                    </td>
                    <td className="tabular-nums">{row.rolls}</td>
                    <td className="text-right tabular-nums">
                      {signed(row.r_multiple)}
                    </td>
                    <td className="text-right tabular-nums">
                      {rupees(row.net_pnl_inr)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ))
      )}
    </section>
  );
}

export function EvidenceCard({ evidence }: { evidence: FnoEvidence }) {
  return (
    <section
      className="space-y-3 rounded-lg border border-border/70 bg-card p-4"
      aria-labelledby="fno-evidence-heading"
      data-testid="fno-evidence"
    >
      <h2 id="fno-evidence-heading" className="text-sm font-semibold">
        The evidence F1 stands on
      </h2>
      <Tier2ECaveat text={evidence.caveat} />
      <p className="text-sm" data-testid="fno-evidence-line">
        {evidence.line}.{" "}
        <span className="text-muted-foreground">n = {evidence.n}.</span>
      </p>
      <p className="text-sm">{evidence.loss_close_line}.</p>
      <p className="text-xs text-muted-foreground">
        {evidence.slippage_note}. Research run{" "}
        {formatTradeDate(evidence.run_date)} over {evidence.sample}.
      </p>
      <div>
        <p className="text-xs font-medium">Why it is only a paper candidate</p>
        <ul className="list-disc pl-5 text-xs text-muted-foreground">
          {evidence.why_paper.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      </div>
      <div data-testid="fno-retest">
        <p className="text-xs font-medium">Latest quarterly re-test</p>
        {evidence.backtests.length === 0 ? (
          <p className="text-xs text-muted-foreground">
            No re-test has run yet. The first runs at the next quarter&rsquo;s
            turn.
          </p>
        ) : (
          evidence.backtests.map((b) => (
            <div key={b.family} className="space-y-1 text-xs">
              <p>
                {b.family}: {signed(b.net_r, 3)}R a trade, n = {b.n},{" "}
                {formatTradeDate(b.sample_from)} to{" "}
                {formatTradeDate(b.sample_to)}, slippage{" "}
                {b.slippage_source.toLowerCase()}.
              </p>
              <Tier2ECaveat text={b.caveat} />
            </div>
          ))
        )}
      </div>
      <div data-testid="fno-tally">
        <p className="text-xs font-medium">
          The paper record against its checklist
        </p>
        <ul className="text-xs">
          {evidence.tally.map((t) => (
            <li key={t.sleeve}>
              <span className="font-medium">
                {SLEEVE_NAME[t.sleeve] ?? t.sleeve}
              </span>
              : {t.closed} closed on paper
              {t.sleeve === "F2"
                ? `, ${t.rolls} rolls`
                : `, ${t.opened_cycles} monthly cycles opened`}
              {t.first_opened
                ? ` since ${formatTradeDate(t.first_opened)}`
                : ""}
              .{" "}
              <span className="text-muted-foreground">
                Needs {t.target}; rule violations{" "}
                {t.violations === null
                  ? "are not counted yet"
                  : String(t.violations)}
                .
              </span>
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}

function F2Candidates({ f2 }: { f2: FnoF2 }) {
  if (f2.sleeve_row) {
    return (
      <div data-testid="fno-f2-sleeve-row" className="text-sm">
        <ToneBadge {...toneProps(f2.sleeve_row.state)} />
        <Reasons reasons={f2.sleeve_row.reasons} />
      </div>
    );
  }
  if (f2.candidates.length === 0) {
    const others = Object.entries(f2.state_counts)
      .map(([state, n]) => `${n} ${stateText(state).label.toLowerCase()}`)
      .join(" · ");
    return (
      <p className="text-sm text-muted-foreground" data-testid="fno-f2-none">
        {f2.scan_date
          ? `No F2 candidate for the next session. ${others}.`
          : "F2 has not been scanned yet."}
      </p>
    );
  }
  return (
    <div className="overflow-x-auto">
      <table
        className="w-full min-w-[36rem] text-sm"
        data-testid="fno-f2-candidates"
      >
        <thead>
          <tr className="text-left text-xs text-muted-foreground">
            <th className="font-normal">Symbol</th>
            <th className="font-normal">State</th>
            <th className="text-right font-normal">Breakout level</th>
            <th className="text-right font-normal">Stop</th>
            <th className="text-right font-normal">R per lot (points)</th>
            <th className="text-right font-normal">One lot risks</th>
          </tr>
        </thead>
        <tbody>
          {f2.candidates.map((c) => {
            const liveRefuses =
              c.state === "CANDIDATE" && c.detail.lots_at_ceiling === 0;
            return (
              <tr key={c.symbol} data-state={c.state}>
                <td>{c.symbol}</td>
                <td>
                  {stateText(c.state).label}
                  {liveRefuses ? (
                    <span className="block text-xs text-muted-foreground">
                      paper one lot; live would be rejected: size
                    </span>
                  ) : null}
                </td>
                <td className="text-right tabular-nums">
                  {price(detailStr(c.detail, "breakout_level"))}
                </td>
                <td className="text-right tabular-nums">
                  {price(detailStr(c.detail, "stop"))}
                </td>
                <td className="text-right tabular-nums">
                  {price(detailStr(c.detail, "risk_per_unit"))}
                </td>
                <td className="text-right tabular-nums">
                  {rupees(detailStr(c.detail, "risk_per_lot_inr"))}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function toneProps(state: string): {
  text: string;
  tone: ReturnType<typeof stateText>["tone"];
} {
  const { label, tone } = stateText(state);
  return { text: label, tone };
}

function F2Open({ positions }: { positions: FnoPosition[] }) {
  if (positions.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">No F2 position is open.</p>
    );
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[40rem] text-sm" data-testid="fno-f2-open">
        <thead>
          <tr className="text-left text-xs text-muted-foreground">
            <th className="font-normal">Symbol</th>
            <th className="text-right font-normal">Entry</th>
            <th className="text-right font-normal">Trailing stop</th>
            <th className="font-normal">GTT id</th>
            <th className="font-normal">Next roll</th>
            <th className="text-right font-normal">Sessions</th>
            <th className="text-right font-normal">P&amp;L</th>
            <th className="text-right font-normal">R</th>
          </tr>
        </thead>
        <tbody>
          {positions.map((p) => (
            <tr key={p.id}>
              <td>
                {p.symbol}
                <span className="block text-xs text-muted-foreground">
                  {markedAtSettle(p.mark?.trade_date ?? null)}
                </span>
              </td>
              <td className="text-right tabular-nums">
                {price(p.entry_price)}
              </td>
              <td className="text-right tabular-nums">
                {price(p.mark?.stop_price ?? p.stop_price)}
              </td>
              <td>{p.gtt_id ?? "none recorded"}</td>
              <td className="tabular-nums">
                {formatTradeDate(p.next_roll_date)}
              </td>
              <td className="text-right tabular-nums">
                {p.sessions_held ?? "—"}
              </td>
              <td className="text-right tabular-nums">
                {rupees(p.mark?.pnl_inr ?? null)}
              </td>
              <td className="text-right tabular-nums">
                {signed(p.mark?.pnl_r ?? null)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function F2Section({ f2 }: { f2: FnoF2 }) {
  return (
    <section
      className="space-y-4"
      aria-labelledby="fno-f2-heading"
      data-testid="fno-f2"
    >
      <h2
        id="fno-f2-heading"
        className="text-sm font-medium uppercase tracking-wide text-muted-foreground"
      >
        F2 · Stock-futures breakout, long only (paper)
      </h2>
      <F2AgainstResearchBanner />
      <p className="text-xs text-muted-foreground">{f2.research_line}.</p>
      <div className="space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="text-sm font-medium">
            Candidates for the next session
          </h3>
          <span className="text-xs text-muted-foreground">
            {asOfClose(f2.scan_date)}
          </span>
        </div>
        <F2Candidates f2={f2} />
      </div>
      <div className="space-y-2">
        <h3 className="text-sm font-medium">Open positions</h3>
        <F2Open positions={f2.open} />
      </div>
      <JournalTables rows={f2.closed} title="F2 closed trades" />
      <DeskOnlyPrices />
    </section>
  );
}
