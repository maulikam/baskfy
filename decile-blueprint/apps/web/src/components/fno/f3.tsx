import { ToneBadge } from "@/components/options/state-chip";
import { LiveMarksProvider } from "@/components/screens/live-price";
import { formatTradeDate } from "@/lib/format";
import type {
  FnoF3,
  FnoPosition,
  FnoScan,
  FnoUnderlying,
} from "@/lib/fno/types";
import {
  SLEEVE_NAME,
  asOfClose,
  detailStr,
  f3DirectionText,
  f3ExpiryKind,
  markedAtSettle,
  price,
  rupees,
  signed,
  stateText,
} from "@/lib/fno/view";

import { DeskOnlyPrices, Tier2ECaveat } from "./caveats";
import {
  Fact,
  JournalTables,
  PositionLegs,
  Reasons,
  legsOf,
  nested,
} from "./overnight";
import { UnderlyingLevel } from "./underlying-level";

/**
 * F3 on `/options/overnight` (DECISIONS-FO M.5, F3-7): the directional index credit spread —
 * NIFTY on the weekly expiry (F3N), BANKNIFTY on the monthly (F3B).
 *
 * Read-only, like every panel on the page: the plan at 09:20, the minute-by-minute exits and the
 * next-session add live on the desk's `/fno`, which is the only surface with a button. The clock
 * is the page's: the night's read is `As of close`, open spreads are `Marked at settle`, and the
 * index level is the one number that may update live.
 */

const LIVE_SYMBOLS = ["NIFTY 50", "NIFTY BANK"] as const;

/** The F3 exits in `04` §11's precedence, as a reader says them. */
const EXIT_RULES =
  "Exits, checked on the desk every minute in this order: the index trades 0.10 % past the key " +
  "level; the spread's price reaches twice the credit; the price decays 80 %; 15:00 on expiry " +
  "day. Each waits for a click on the desk unless its auto-exit switch is on, which is off by " +
  "default and only Maulik turns on. Entries and adds are always clicks.";

function ProposedSpread({ scan }: { scan: FnoScan }) {
  const legs = legsOf(scan.detail);
  if (legs.length === 0) return null;
  const ORDER = ["LONG_PUT", "SHORT_PUT", "SHORT_CALL", "LONG_CALL"];
  const sorted = [...legs].sort(
    (a, b) => ORDER.indexOf(a.role) - ORDER.indexOf(b.role),
  );
  const expiry = detailStr(scan.detail, "expiry");
  const sizing = nested(scan.detail, "sizing", "message");
  return (
    <div className="space-y-2" data-testid="fno-f3-spread">
      <h4 className="text-sm font-medium">Proposed spread</h4>
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
        <Fact label="Expiry" value={expiry ? formatTradeDate(expiry) : "—"} />
        <Fact
          label="Credit (points)"
          value={price(detailStr(scan.detail, "credit_points"))}
        />
        <Fact
          label="Max loss per lot"
          value={rupees(detailStr(scan.detail, "max_loss_per_lot_inr"))}
        />
        <Fact
          label="Decay target (price)"
          value={price(detailStr(scan.detail, "decay_target_mark"))}
        />
        <Fact
          label="Loss cut (price)"
          value={price(detailStr(scan.detail, "loss_cut_mark"))}
        />
        <Fact
          label="Key level"
          value={price(detailStr(scan.detail, "level"))}
        />
      </dl>
      {sizing ? (
        <p className="text-xs text-muted-foreground">{sizing}</p>
      ) : null}
    </div>
  );
}

function Levels({ scan }: { scan: FnoScan }) {
  const read = (field: string) => price(nested(scan.detail, "levels", field));
  if (nested(scan.detail, "levels", "close") === null) return null;
  const confirm = nested(scan.detail, "confirm", "message");
  const daily =
    detailStr(scan.detail, "direction_daily") ??
    detailStr(scan.detail, "direction");
  return (
    <div className="space-y-2" data-testid="fno-f3-levels">
      <p className="text-sm">
        <span className="text-muted-foreground">Daily chart: </span>
        <span data-testid="fno-f3-direction">{f3DirectionText(daily)}</span>
      </p>
      {confirm ? (
        <p
          className="text-xs text-muted-foreground"
          data-testid="fno-f3-confirm"
        >
          75-minute confirm: {confirm}
        </p>
      ) : null}
      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm sm:grid-cols-3">
        <Fact label="Close" value={read("close")} />
        <Fact label="Support" value={read("support")} />
        <Fact label="Resistance" value={read("resistance")} />
        <Fact label="20-day average" value={read("trend_avg")} />
        <Fact
          label="Weekly range"
          value={`${read("weekly_low")} – ${read("weekly_high")}`}
        />
      </dl>
    </div>
  );
}

export function F3Card({
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
      data-testid={`fno-f3-${underlying.symbol}`}
    >
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <h3 className="text-base font-semibold">
            {underlying.symbol}{" "}
            <span className="text-xs font-normal text-muted-foreground">
              {f3ExpiryKind(underlying.symbol)} · {underlying.sleeve}
            </span>
          </h3>
          <p className="text-xs text-muted-foreground">
            The desk may plan it at 09:20 on{" "}
            <span className="text-foreground" data-testid="fno-f3-next">
              {underlying.next_entry_date
                ? formatTradeDate(underlying.next_entry_date)
                : "no session known yet"}
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
              testId="fno-f3-state"
            />
            <span className="text-xs text-muted-foreground">
              {asOfClose(scanDate)}
            </span>
          </div>
          <Reasons reasons={scan.reasons} />
          <Levels scan={scan} />
          <ProposedSpread scan={scan} />
        </>
      ) : (
        <p
          className="text-sm text-muted-foreground"
          data-testid="fno-f3-unscanned"
        >
          Not scanned yet.
        </p>
      )}
    </article>
  );
}

function F3Open({ positions }: { positions: FnoPosition[] }) {
  if (positions.length === 0) {
    return (
      <p className="text-sm text-muted-foreground" data-testid="fno-f3-no-open">
        No F3 spread is open.
      </p>
    );
  }
  return (
    <ul className="grid gap-3 lg:grid-cols-2">
      {positions.map((p) => (
        <li
          key={p.id}
          className="space-y-2 rounded-lg border border-border/70 bg-card p-4"
          data-testid="fno-f3-open"
        >
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="font-medium">
              {SLEEVE_NAME[p.sleeve] ?? p.sleeve} · {p.lots} lot
              {p.lots === 1 ? "" : "s"} · {p.simulated ? "paper" : "live"}
            </span>
            <span className="text-xs text-muted-foreground">
              {markedAtSettle(p.mark?.trade_date ?? null)}
            </span>
          </div>
          <PositionLegs legs={p.legs} />
          <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm sm:grid-cols-3">
            <Fact label="Credit taken" value={price(p.entry_credit)} />
            <Fact
              label="Price (settle)"
              value={price(p.mark?.mark_points ?? null)}
            />
            <Fact label="P&L" value={rupees(p.mark?.pnl_inr ?? null)} />
            <Fact label="P&L in R" value={signed(p.mark?.pnl_r ?? null)} />
            <Fact label="Key level" value={price(p.stop_price)} />
            <Fact
              label="Decay target (price)"
              value={price(p.profit_take_points)}
            />
            <Fact label="Loss cut (price)" value={price(p.loss_close_points)} />
            <Fact
              label="Days held"
              value={p.sessions_held === null ? "—" : String(p.sessions_held)}
            />
          </dl>
        </li>
      ))}
    </ul>
  );
}

function F3Evidence({ f3 }: { f3: FnoF3 }) {
  return (
    <div
      className="space-y-3 rounded-lg border border-border/70 bg-card p-4"
      data-testid="fno-f3-evidence"
    >
      <h3 className="text-sm font-semibold">The evidence F3 stands on</h3>
      <p className="text-sm" data-testid="fno-f3-research">
        {f3.research_line}.
      </p>
      <div>
        <p className="text-xs font-medium">
          What the closing files cannot test
        </p>
        <ul
          className="list-disc pl-5 text-xs text-muted-foreground"
          data-testid="fno-f3-not-tested"
        >
          {f3.not_tested.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      </div>
      <div data-testid="fno-f3-retest">
        <p className="text-xs font-medium">Latest quarterly re-test</p>
        {f3.backtests.length === 0 ? (
          <p className="text-xs text-muted-foreground">
            No re-test has run yet. The first runs at the next quarter&rsquo;s
            turn.
          </p>
        ) : (
          f3.backtests.map((b) => (
            <div key={b.family} className="space-y-1 text-xs">
              <p>
                {SLEEVE_NAME[b.family] ?? b.family}: {signed(b.net_r, 3)}R a
                trade, n = {b.n}, {formatTradeDate(b.sample_from)} to{" "}
                {formatTradeDate(b.sample_to)}.
              </p>
              <Tier2ECaveat text={b.caveat} />
            </div>
          ))
        )}
      </div>
    </div>
  );
}

export function F3Section({ f3 }: { f3: FnoF3 }) {
  return (
    <section
      className="space-y-4"
      aria-labelledby="fno-f3-heading"
      data-testid="fno-f3"
    >
      <h2
        id="fno-f3-heading"
        className="text-sm font-medium uppercase tracking-wide text-muted-foreground"
      >
        F3 · Directional index credit spread (paper)
      </h2>
      <p className="text-xs text-muted-foreground" data-testid="fno-f3-exits">
        {EXIT_RULES}
      </p>
      <LiveMarksProvider symbols={LIVE_SYMBOLS}>
        <div className="grid gap-3 lg:grid-cols-2">
          {f3.underlyings.map((underlying) => (
            <F3Card
              key={underlying.symbol}
              underlying={underlying}
              scanDate={f3.scan_date}
            />
          ))}
        </div>
      </LiveMarksProvider>
      <div className="space-y-2">
        <h3 className="text-sm font-medium">Open spreads</h3>
        <F3Open positions={f3.open} />
      </div>
      <JournalTables rows={f3.closed} title="F3 closed spreads" />
      <F3Evidence f3={f3} />
      <DeskOnlyPrices />
    </section>
  );
}
