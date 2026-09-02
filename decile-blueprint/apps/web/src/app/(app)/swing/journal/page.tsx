import type { Metadata } from "next";

import { Answer, Mark } from "@/components/shell/answer";
import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { formatDateTimeIST, formatTradeDate } from "@/lib/format";
import {
  fetchJournal,
  type SwingBacktestCard,
  type SwingJournalCard,
  type SwingLadder,
  type SwingSessions,
} from "@/lib/swing/fetch";
import { PAGES } from "@/lib/vocabulary";

import { GATE_UNKNOWN, RUNGS, ladderSentence, money, signedInr, signedR } from "./copy";

/**
 * `/swing/journal` — the Journal tab of `docs/swing/05` §2.
 *
 * "The journal tells him, in R, whether he should be pressing or sitting." Four things, each
 * answering a different question:
 *
 * - **Two cards, real and simulated, never mixed** (`04` §10). A paper streak and a real streak
 *   are facts about different money; a card that averaged them would be a number nobody could
 *   act on. So the simulated card is a separate component instance fed a separate object, and
 *   the test asserts a simulated close never appears in the real card.
 * - **The ladder** as it stands tonight, and the one sentence that says what the next close
 *   does to it (`04` §8.4) — the rule that applies, not the rule book.
 * - **The session count** against the twenty-session paper gate (`02` §3.2).
 * - **The backtest card** under `02` §3.3's own heading, with `04` §11's caveats verbatim when a
 *   run exists and an honest "not run yet" when none does.
 *
 * Read-only. No form, no action, nothing that reaches an order path; a close is written by the
 * desk console when a line is confirmed there, and this page reads what it wrote.
 */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/swing/journal"].title,
  description: PAGES["/swing/journal"].blurb,
};

const HEAD_CELL = "py-2 pr-3 font-medium";
const HEAD_ROW =
  "border-b border-border/70 text-left text-xs uppercase tracking-wide text-muted-foreground";
const BODY_ROW = "border-b border-border/40";

function reason(value: string | null): string {
  return value ? value.replaceAll("_", " ").toLowerCase() : "—";
}

function setupName(value: string): string {
  return value.replaceAll("_", " ");
}

/** `2026-08` → `Aug 2026`, the way the trade dates read; an unexpected shape is shown as sent. */
function monthLabel(month: string): string {
  const parsed = new Date(`${month}-01T00:00:00Z`);
  if (Number.isNaN(parsed.getTime())) return month;
  return new Intl.DateTimeFormat("en-GB", { month: "short", year: "numeric", timeZone: "UTC" }).format(
    parsed,
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="space-y-0.5">
      <dt className="text-xs uppercase tracking-wide text-muted-foreground">{label}</dt>
      <dd className="tabular-nums text-sm font-medium">{value}</dd>
    </div>
  );
}

/**
 * The R distribution: six bars in the API's order, drawn as counts rather than as a density so
 * a record of three trades reads as three trades. At zero trades every bar is empty and the
 * caption says so — a flat chart with no words is a chart that failed to load.
 */
function Histogram({ card, id }: { card: SwingJournalCard; id: string }) {
  const most = Math.max(0, ...card.histogram.map((bar) => bar.count));
  return (
    <div className="space-y-2" data-testid={`${id}-histogram`}>
      <h4 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
        R distribution
      </h4>
      <ul className="space-y-1" aria-label="Trades by R bucket">
        {card.histogram.map((bar) => {
          const losing = bar.bucket.startsWith("<") || bar.bucket.startsWith("-");
          const width = most > 0 ? (bar.count / most) * 100 : 0;
          return (
            <li
              key={bar.bucket}
              className="grid grid-cols-[4rem_1fr_2.5rem] items-center gap-2 text-sm"
              aria-label={`${bar.bucket}: ${bar.count} ${bar.count === 1 ? "trade" : "trades"}`}
            >
              <span className="tabular-nums text-muted-foreground">{bar.bucket}</span>
              <span className="h-3 w-full overflow-hidden rounded-sm bg-muted" aria-hidden="true">
                <span
                  className={`block h-full ${losing ? "bg-negative" : "bg-positive"}`}
                  style={{ width: `${width}%` }}
                />
              </span>
              <span className="tabular-nums text-right" data-testid={`${id}-bucket-${bar.bucket}`}>
                {bar.count}
              </span>
            </li>
          );
        })}
      </ul>
      {card.stats.trades === 0 ? (
        <p className="text-xs text-muted-foreground">No closed trades yet — every bar is empty.</p>
      ) : null}
    </div>
  );
}

/**
 * One record. `title` is the only thing the two instances share; each is handed its own card
 * and reads nothing outside it.
 */
function JournalCard({
  id,
  title,
  blurb,
  card,
}: {
  id: "real" | "simulated";
  title: string;
  blurb: string;
  card: SwingJournalCard;
}) {
  const { stats } = card;
  const streak = stats.current_loss_streak;
  return (
    <section
      aria-label={title}
      data-testid={`journal-card-${id}`}
      className="space-y-5 rounded-lg border border-border bg-card p-4"
    >
      <div className="space-y-1">
        <h3 className="text-lg font-medium">{title}</h3>
        <p className="max-w-[60ch] text-sm text-muted-foreground">{blurb}</p>
      </div>

      <dl
        className="grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-3 lg:grid-cols-5"
        data-testid={`${id}-stats`}
      >
        <Stat label="Closed trades" value={String(stats.trades)} />
        <Stat label="Net R" value={signedR(stats.net_r)} />
        <Stat label="Expectancy" value={`${signedR(stats.expectancy_r)} a trade`} />
        <Stat label="Win rate" value={`${stats.win_rate_pct.toFixed(0)}%`} />
        <Stat
          label="Profit factor"
          value={stats.profit_factor === null ? "—" : stats.profit_factor.toFixed(2)}
        />
        <Stat label="Average win" value={signedR(stats.avg_win_r)} />
        <Stat label="Average loss" value={signedR(stats.avg_loss_r)} />
        <Stat label="Largest win" value={signedR(stats.largest_win_r)} />
        <Stat label="Largest loss" value={signedR(stats.largest_loss_r)} />
        <Stat
          label="Loss streak"
          value={streak === 0 ? "none" : `${streak} in a row`}
        />
      </dl>
      {stats.profit_factor === null && stats.trades > 0 ? (
        <p className="text-xs text-muted-foreground">
          No profit factor yet: it is gross win R over gross loss R, and there is no loss to
          measure against.
        </p>
      ) : null}

      <Histogram card={card} id={id} />

      <div className="grid gap-6 md:grid-cols-2">
        <div className="space-y-2">
          <h4 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            By setup
          </h4>
          {card.by_setup.length === 0 ? (
            <p className="text-sm text-muted-foreground">No closed trades yet.</p>
          ) : (
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className={HEAD_ROW}>
                  <th className={HEAD_CELL}>Setup</th>
                  <th className={HEAD_CELL}>Trades</th>
                  <th className={HEAD_CELL}>Net R</th>
                  <th className={HEAD_CELL}>Expectancy</th>
                </tr>
              </thead>
              <tbody>
                {card.by_setup.map((row) => (
                  <tr key={row.setup} className={BODY_ROW}>
                    <td className="py-2 pr-3 font-medium">{setupName(row.setup)}</td>
                    <td className="py-2 pr-3 tabular-nums">{row.trades}</td>
                    <td className="py-2 pr-3 tabular-nums">{signedR(row.net_r)}</td>
                    <td className="py-2 pr-3 tabular-nums">{signedR(row.expectancy_r)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        <div className="space-y-2">
          <h4 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            By month
          </h4>
          {card.by_month.length === 0 ? (
            <p className="text-sm text-muted-foreground">No month has a closed trade yet.</p>
          ) : (
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className={HEAD_ROW}>
                  <th className={HEAD_CELL}>Month closed</th>
                  <th className={HEAD_CELL}>Trades</th>
                  <th className={HEAD_CELL}>Net R</th>
                </tr>
              </thead>
              <tbody>
                {card.by_month.map((row) => (
                  <tr key={row.month} className={BODY_ROW}>
                    <td className="py-2 pr-3 tabular-nums">{monthLabel(row.month)}</td>
                    <td className="py-2 pr-3 tabular-nums">{row.trades}</td>
                    <td className="py-2 pr-3 tabular-nums">{signedR(row.net_r)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      <div className="space-y-2">
        <h4 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
          Every closed trade, newest first
          {stats.trades > card.trades.length
            ? ` (the latest ${card.trades.length} of ${stats.trades}; the statistics above cover all of them)`
            : ""}
        </h4>
        {card.trades.length === 0 ? (
          <p className="text-sm text-muted-foreground">Nothing has closed yet.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[56rem] border-collapse text-sm">
              <thead>
                <tr className={HEAD_ROW}>
                  <th className={HEAD_CELL}>Stock</th>
                  <th className={HEAD_CELL}>Setup</th>
                  <th className={HEAD_CELL}>Entered</th>
                  <th className={HEAD_CELL}>Closed</th>
                  <th className={HEAD_CELL}>Entry</th>
                  <th className={HEAD_CELL}>Initial stop</th>
                  <th className={HEAD_CELL}>Exit</th>
                  <th className={HEAD_CELL}>Qty</th>
                  <th className={HEAD_CELL}>R</th>
                  <th className={HEAD_CELL}>P&amp;L</th>
                  <th className={HEAD_CELL}>Why</th>
                </tr>
              </thead>
              <tbody>
                {card.trades.map((trade, index) => (
                  <tr
                    key={`${trade.symbol}-${trade.exit_date}-${index}`}
                    className={BODY_ROW}
                    data-testid={`${id}-trade`}
                  >
                    <td className="py-2 pr-3 font-medium">{trade.symbol}</td>
                    <td className="py-2 pr-3">{setupName(trade.setup)}</td>
                    <td className="py-2 pr-3 tabular-nums">{formatTradeDate(trade.entry_date)}</td>
                    <td className="py-2 pr-3 tabular-nums">{formatTradeDate(trade.exit_date)}</td>
                    <td className="py-2 pr-3 tabular-nums">{money(trade.entry)}</td>
                    <td className="py-2 pr-3 tabular-nums">{money(trade.initial_stop)}</td>
                    <td className="py-2 pr-3 tabular-nums">{money(trade.exit_avg)}</td>
                    <td className="py-2 pr-3 tabular-nums">{trade.quantity}</td>
                    <td
                      className={`py-2 pr-3 tabular-nums ${
                        trade.r_multiple < 0 ? "text-negative" : trade.r_multiple > 0 ? "text-positive" : ""
                      }`}
                    >
                      {signedR(trade.r_multiple)}
                    </td>
                    <td className="py-2 pr-3 tabular-nums">{signedInr(trade.pnl_inr)}</td>
                    <td className="py-2 pr-3 text-xs text-muted-foreground">
                      {reason(trade.close_reason)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </section>
  );
}

function Sessions({ sessions }: { sessions: SwingSessions }) {
  const required = Math.max(sessions.required, 1);
  const percent = Math.min(100, Math.round((sessions.logged / required) * 100));
  const line = `${sessions.logged} of ${sessions.required} paper sessions logged`;
  return (
    <section aria-label="Paper sessions" className="space-y-2" data-testid="journal-sessions">
      <h2 className="text-sm font-medium uppercase tracking-wide text-muted-foreground">
        The paper record
      </h2>
      <p className="text-sm font-medium">{line}</p>
      <div
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={sessions.required}
        aria-valuenow={Math.min(sessions.logged, sessions.required)}
        aria-label={line}
        className="h-2 w-full max-w-md overflow-hidden rounded-full bg-muted"
      >
        <div className="h-full rounded-full bg-accent" style={{ width: `${percent}%` }} />
      </div>
      <p className="max-w-[70ch] text-sm text-muted-foreground">
        Real money waits on {sessions.required} dry-run sessions with their plans, confirms,
        simulated fills and stop actions on record, and on the rest of the real-money gate — none
        of which this page can flip.
      </p>
    </section>
  );
}

function Ladder({ ladder }: { ladder: SwingLadder }) {
  const rung = ladder.level + 1;
  const reads = ladder.reads === "REAL" ? "the real record" : "the simulated record";
  return (
    <section aria-label="The ladder" className="space-y-3" data-testid="journal-ladder">
      <h2 className="text-lg font-medium">The ladder</h2>
      <dl className="grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-4">
        <Stat label="Rung" value={`${rung} of ${RUNGS}`} />
        <Stat
          label="Gate"
          value={ladder.gate === GATE_UNKNOWN ? "not measured" : ladder.gate}
        />
        <Stat
          label="Allows"
          value={`${ladder.max_open_positions} positions · ${ladder.max_exposure_pct.toFixed(0)}% of the allocation`}
        />
        <Stat
          label="New entries"
          value={ladder.new_entries_allowed ? "allowed" : "not allowed"}
        />
      </dl>
      <p className="max-w-[70ch] text-sm" data-testid="journal-ladder-sentence">
        {ladderSentence(ladder)}
      </p>
      <p className="max-w-[70ch] text-xs text-muted-foreground">
        The ladder reads {reads}
        {ladder.last_r.length === 0 ? (
          ", which has no closed trade yet."
        ) : (
          <>
            {" "}
            — its last {ladder.last_r.length === 1 ? "close" : `${ladder.last_r.length} closes`},
            oldest first:{" "}
            <span className="tabular-nums" data-testid="journal-ladder-last-r">
              {ladder.last_r.map((value) => signedR(value)).join(" · ")}
            </span>
            .
          </>
        )}
      </p>
    </section>
  );
}

/** A value of SW9's `params` / `stats`, whose shape this page does not own, made readable. */
function generic(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") return Number.isInteger(value) ? String(value) : value.toFixed(2);
  if (typeof value === "string" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) return `${value.length} ${value.length === 1 ? "entry" : "entries"}`;
  return `${Object.keys(value).length} fields`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * A key of SW9's JSON as a label. The schema's own words stay in the schema (`sleeve_inr`);
 * what the reader sees follows the product's vocabulary (DECISIONS-SW SW4.2).
 */
function label(key: string): string {
  return key
    .replaceAll("_", " ")
    .replace(/\bsleeve\b/g, "allocation")
    .replace(/\bbook\b/g, "record");
}

/** How many levels of SW9's JSON are opened up before a value is shown as a count. */
const FLATTEN_DEPTH = 2;

/**
 * Top-level values, with nested records opened up as `group · key` down to `FLATTEN_DEPTH` —
 * enough for a statistics row and a `by_setup` / `by_year` table of them, without pretending
 * to know SW9's shape. A deeper value, or a series, is shown as a count rather than dumped.
 */
function flatten(record: Record<string, unknown>, prefix = "", depth = 0): [string, string][] {
  const out: [string, string][] = [];
  for (const [key, value] of Object.entries(record)) {
    const name = prefix ? `${prefix} · ${label(key)}` : label(key);
    if (isRecord(value) && depth < FLATTEN_DEPTH) {
      out.push(...flatten(value, name, depth + 1));
    } else {
      out.push([name, generic(value)]);
    }
  }
  return out;
}

function GenericList({ title, record }: { title: string; record: Record<string, unknown> }) {
  const entries = flatten(record);
  return (
    <div className="space-y-2">
      <h3 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{title}</h3>
      {entries.length === 0 ? (
        <p className="text-sm text-muted-foreground">Nothing recorded.</p>
      ) : (
        <dl className="grid grid-cols-2 gap-x-6 gap-y-2 sm:grid-cols-3 lg:grid-cols-4">
          {entries.map(([label, value]) => (
            <Stat key={label} label={label} value={value} />
          ))}
        </dl>
      )}
    </div>
  );
}

function Backtest({ backtest }: { backtest: SwingBacktestCard | null }) {
  return (
    <section
      aria-label="Backtest, EOD approximation"
      className="space-y-4"
      data-testid="journal-backtest"
    >
      <div className="space-y-1">
        <h2 className="text-lg font-medium">Backtest, EOD approximation</h2>
        <p className="max-w-[70ch] text-sm text-muted-foreground">
          The rules replayed over published end-of-day bars, with the ladder in force and costs
          on both sides. An approximation, and labelled as one: the caveats are the reasons it
          is not the desk.
        </p>
      </div>
      {backtest === null ? (
        <p className="text-sm" data-testid="journal-backtest-empty">
          Not run yet. When the backtest runner has stored a run, its R distribution, win rate and
          expectancy appear here with their caveats; until then there is nothing to show, and the
          real-money gate stays shut on this count.
        </p>
      ) : (
        <div className="space-y-5">
          <p className="text-sm text-muted-foreground">
            Run {backtest.run_id}, started {formatDateTimeIST(backtest.started_at)}
            {backtest.finished_at
              ? `, finished ${formatDateTimeIST(backtest.finished_at)}.`
              : " — still running, or never finished."}
          </p>
          <div className="space-y-2">
            <h3 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              Caveats
            </h3>
            {backtest.caveats.length === 0 ? (
              <p className="text-sm text-muted-foreground">The run recorded no caveats.</p>
            ) : (
              <ul className="list-disc space-y-1 pl-5 text-sm" data-testid="journal-backtest-caveats">
                {backtest.caveats.map((caveat, index) => (
                  <li key={`${index}-${caveat}`}>{caveat}</li>
                ))}
              </ul>
            )}
          </div>
          <GenericList title="Results" record={backtest.stats} />
          <GenericList title="Parameters" record={backtest.params} />
        </div>
      )}
    </section>
  );
}

/** The sentence at the top: the record the ladder reads, in one line, or why there is none. */
function Verdict({
  card,
  reads,
  sessions,
}: {
  card: SwingJournalCard;
  reads: SwingLadder["reads"];
  sessions: SwingSessions;
}) {
  const which = reads === "REAL" ? "real" : "simulated";
  if (card.stats.trades === 0) {
    return (
      <>
        No {which} trade has closed yet —{" "}
        <Mark>
          {sessions.logged} of {sessions.required} paper sessions
        </Mark>{" "}
        logged, and the ladder is reading an empty record.
      </>
    );
  }
  const { stats } = card;
  return (
    <>
      The {which} record stands at{" "}
      <Mark>
        {signedR(stats.net_r)} over {stats.trades} {stats.trades === 1 ? "trade" : "trades"}
      </Mark>
      , {signedR(stats.expectancy_r)} a trade,{" "}
      {stats.current_loss_streak === 0
        ? "with no losing streak running."
        : `with ${stats.current_loss_streak} ${stats.current_loss_streak === 1 ? "loss" : "losses"} in a row behind it.`}
    </>
  );
}

export default async function SwingJournalPage() {
  const journal = await fetchJournal();

  return (
    <div className="space-y-8">
      <PageHeader
        title={PAGES["/swing/journal"].title}
        blurb={PAGES["/swing/journal"].blurb}
        meta={
          journal ? (
            <span className="text-sm text-muted-foreground">
              {journal.sessions.logged} of {journal.sessions.required} paper sessions logged
            </span>
          ) : null
        }
      />
      <SectionTabs section="swing" />

      <Answer
        footnote={
          "Simulated and real results are never added together: each card is its own record. " +
          "Nothing on this page can place an order — a close is written when a line is confirmed " +
          "in the desk console."
        }
      >
        {journal ? (
          <Verdict
            card={journal.ladder.reads === "REAL" ? journal.real : journal.simulated}
            reads={journal.ladder.reads}
            sessions={journal.sessions}
          />
        ) : (
          <>The journal cannot be read right now, so there is nothing to say about the record.</>
        )}
      </Answer>

      {journal ? (
        <>
          <Sessions sessions={journal.sessions} />
          <Ladder ladder={journal.ladder} />

          <section aria-label="Results" className="space-y-4">
            <h2 className="text-lg font-medium">Results, in R</h2>
            <div className="grid gap-6 xl:grid-cols-2">
              <JournalCard
                id="real"
                title="Real"
                blurb="Closes of positions bought with money. Empty until execution is enabled on the server, and the ladder reads it only from then."
                card={journal.real}
              />
              <JournalCard
                id="simulated"
                title="Simulated"
                blurb="Closes of dry-run positions confirmed in the desk console — simulated fills, no broker. The paper record the real-money gate is judged on."
                card={journal.simulated}
              />
            </div>
          </section>

          <Backtest backtest={journal.backtest} />
        </>
      ) : (
        <section aria-label="Journal unavailable" className="space-y-2" data-testid="journal-empty">
          <p className="max-w-[70ch] text-sm text-muted-foreground">
            The API did not answer, or has no journal to give. If the swing routes have not been
            deployed on this server there is nothing to show yet; if they have, the record is
            still there and this page will read it on the next load.
          </p>
        </section>
      )}
    </div>
  );
}
