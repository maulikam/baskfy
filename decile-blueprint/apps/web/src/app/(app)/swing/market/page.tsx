import type { Metadata } from "next";

import { Answer, Mark } from "@/components/shell/answer";
import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import { formatTradeDate } from "@/lib/format";
import { fetchMarket, type SwingMarketDay } from "@/lib/swing/fetch";
import { PAGES } from "@/lib/vocabulary";

import { breadthLine, indexWord, tierLine } from "../copy";

/**
 * `/swing/market` — the Market tab of `docs/swing/05` §2.
 *
 * Two questions, in order: **may the book trade at all**, and **how much may it carry**. The
 * first is `04` §8.3's gate, decided by breadth with the index able only to make it worse. The
 * second is §8.4's ladder, which reads the trader's own last five closed trades — so on a page
 * with no closed trades yet it says so rather than implying the rung means something.
 *
 * `05` §2 asks for a line saying **what would change the gate**, and that line is the point of
 * the page: a gate is only useful if you can see how far from the other side of it you are.
 *
 * SW14 completes `05` §2's list: the gate as a colour band over time, the rung and the
 * allocation's drawdown session by session with the lock-out shaded (`04` §8.5), the parabolic
 * count, and the index with its 10- and 20-day averages.
 *
 * Read-only. No form, no action, nothing that reaches an order path.
 */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/swing/market"].title,
  description: PAGES["/swing/market"].blurb,
};

/** `04` §8.3's two thresholds, so the "what would change the gate" line is not a magic number. */
const GREEN_MIN_PCT_UP = 5.0;
const RED_MAX_PCT_UP = 2.0;

/** How many sessions the page shows. A quarter is enough to see a regime change happen. */
const SESSIONS = 60;

function percent(value: number | null): string {
  return value === null ? "—" : `${value.toFixed(2)}%`;
}

function price(value: number | null): string {
  return value === null ? "—" : value.toFixed(2);
}

/** The band's colour per gate — the `market/mood` conventions: green, amber, red, grey for unknown. */
const BAND: Record<string, string> = {
  GREEN: "bg-emerald-500",
  AMBER: "bg-amber-400",
  RED: "bg-red-500",
};

/** `05` §2: "the gate as a colour band over time … with the lock-out shaded". */
function GateBand({ days }: { days: SwingMarketDay[] }) {
  return (
    <ol
      aria-label="The gate, session by session"
      className="flex h-6 w-full gap-px overflow-hidden rounded-sm"
      data-testid="gate-band"
    >
      {days.map((day) => (
        <li
          key={day.date}
          title={`${formatTradeDate(day.date)}: ${day.gate} · rung ${day.exposure_level + 1} of 4${day.drawdown_locked ? " · locked out" : ""}`}
          data-gate={day.gate}
          data-locked={day.drawdown_locked ? "true" : undefined}
          className={`flex-1 ${BAND[day.gate] ?? "bg-muted"} ${day.drawdown_locked ? "border-b-4 border-red-800 opacity-60" : ""}`}
        />
      ))}
    </ol>
  );
}

/**
 * `05` §2: "GREEN needs ≥ 5.0% of names up 25% in a month; today 3.8%".
 *
 * Stated as a distance rather than as a verdict, because the verdict is already on the badge and
 * what a person wants next is how close it is.
 */
function whatWouldChangeIt(day: SwingMarketDay): string {
  const today = day.pct_up_strong_1m;
  if (today === null) return "Breadth has not been measured for this session.";
  const index = indexWord(day);
  if (day.gate === "GREEN") {
    const room = (today - RED_MAX_PCT_UP).toFixed(1);
    return `GREEN holds while at least ${GREEN_MIN_PCT_UP.toFixed(1)}% of liquid names are up 25% in a month and the 10-day stays above the 20-day; today ${today.toFixed(1)}%, ${index}. It turns RED below ${RED_MAX_PCT_UP.toFixed(1)}% — ${room} points of room.`;
  }
  if (day.gate === "RED") {
    return `RED while ${RED_MAX_PCT_UP.toFixed(1)}% or fewer of liquid names are up 25% in a month, or the 10-day sits below the 20-day; today ${today.toFixed(1)}%, ${index}.`;
  }
  return `GREEN needs at least ${GREEN_MIN_PCT_UP.toFixed(1)}% of liquid names up 25% in a month and the 10-day above the 20-day; today ${today.toFixed(1)}%, ${index}.`;
}

export default async function SwingMarketPage() {
  const market = await fetchMarket({});
  const days = (market?.data ?? []).slice(-SESSIONS);
  const latest = days.length > 0 ? days[days.length - 1] : null;

  return (
    <div className="space-y-8">
      <PageHeader
        title={PAGES["/swing/market"].title}
        blurb={PAGES["/swing/market"].blurb}
        meta={
          latest ? (
            <span className="text-sm text-muted-foreground">
              As of {formatTradeDate(latest.date)}
            </span>
          ) : null
        }
      />
      <SectionTabs section="swing" />

      <Answer
        footnote={
          "Breadth is measured over the swing allocation's own liquid universe, not over an index. " +
          "The rung reads the last five closed trades; before there are five, it stays at the bottom."
        }
      >
        {latest ? (
          <>
            The tape is <Mark>{latest.gate}</Mark>. <Mark>{tierLine(latest)}</Mark>.{" "}
            {latest.drawdown_locked
              ? "No new entries until the allocation is back inside the line."
              : latest.new_entries_allowed
                ? "New entries are allowed."
                : "No new entries — manage what is open."}
          </>
        ) : (
          <>No market row has been written yet, so there is nothing to say about the tape.</>
        )}
      </Answer>

      {latest ? (
        <section aria-label="What would change the gate" className="space-y-2">
          <h2 className="text-sm font-medium uppercase tracking-wide text-muted-foreground">
            What would change it
          </h2>
          <p className="max-w-[70ch] text-sm">{whatWouldChangeIt(latest)}</p>
          <p className="max-w-[70ch] text-sm text-muted-foreground" data-testid="gate-detail">
            {latest.gate} because {breadthLine(latest)}.
          </p>
        </section>
      ) : null}

      {days.length > 0 ? (
        <section aria-label="The gate over time" className="space-y-2">
          <h2 className="text-sm font-medium uppercase tracking-wide text-muted-foreground">
            The gate over {days.length} sessions
          </h2>
          <GateBand days={days} />
          <p className="text-xs text-muted-foreground">
            Oldest on the left. A dark underline is a session the drawdown lock-out held.
          </p>
        </section>
      ) : null}

      <section aria-label="Breadth history" className="space-y-3">
        <h2 className="text-lg font-medium">Session by session</h2>
        {days.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            Nothing has been recorded yet. The nightly job writes one row a session; the first
            one appears the evening after it first runs.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[48rem] border-collapse text-sm">
              <thead>
                <tr className="border-b border-border/70 text-left text-xs uppercase tracking-wide text-muted-foreground">
                  <th className="py-2 pr-3 font-medium">Session</th>
                  <th className="py-2 pr-3 font-medium">Gate</th>
                  <th className="py-2 pr-3 font-medium">Up 25% in a month</th>
                  <th className="py-2 pr-3 font-medium">At a year high</th>
                  <th className="py-2 pr-3 font-medium">Above the 20-day</th>
                  <th className="py-2 pr-3 font-medium">Liquid names</th>
                  <th className="py-2 pr-3 font-medium">Rung</th>
                  <th className="py-2 pr-3 font-medium">Drawdown</th>
                  <th className="py-2 pr-3 font-medium">Parabolic</th>
                  <th className="py-2 pr-3 font-medium">Index</th>
                  <th className="py-2 pr-3 font-medium">10-day</th>
                  <th className="py-2 pr-3 font-medium">20-day</th>
                </tr>
              </thead>
              <tbody>
                {[...days].reverse().map((day) => (
                  <tr
                    key={day.date}
                    className={
                      day.drawdown_locked
                        ? "border-b border-border/40 bg-red-500/10"
                        : "border-b border-border/40"
                    }
                    data-locked={day.drawdown_locked ? "true" : undefined}
                  >
                    <td className="py-2 pr-3 tabular-nums">{formatTradeDate(day.date)}</td>
                    <td className="py-2 pr-3">{day.gate}</td>
                    <td className="py-2 pr-3 tabular-nums">{percent(day.pct_up_strong_1m)}</td>
                    <td className="py-2 pr-3 tabular-nums">{percent(day.pct_new_52w_high)}</td>
                    <td className="py-2 pr-3 tabular-nums">{percent(day.pct_above_ma_slow)}</td>
                    <td className="py-2 pr-3 tabular-nums">
                      {day.constituent_count.toLocaleString("en-IN")}
                    </td>
                    <td className="py-2 pr-3 tabular-nums">{day.exposure_level + 1} of 4</td>
                    <td className="py-2 pr-3 tabular-nums">
                      {percent(day.drawdown_pct)}
                      {day.drawdown_locked ? (
                        <span className="ml-1.5 text-xs font-medium text-red-700">locked out</span>
                      ) : null}
                    </td>
                    <td className="py-2 pr-3 tabular-nums">{day.parabolic_count}</td>
                    <td className="py-2 pr-3 tabular-nums">{price(day.index_close)}</td>
                    <td className="py-2 pr-3 tabular-nums">{price(day.index_ma_fast)}</td>
                    <td className="py-2 pr-3 tabular-nums">{price(day.index_ma_slow)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {latest?.index_slug ? (
        <section aria-label="The index reading" className="space-y-2">
          <h2 className="text-sm font-medium uppercase tracking-wide text-muted-foreground">
            The index the gate read
          </h2>
          <p className="max-w-[70ch] text-sm text-muted-foreground">
            {latest.index_slug} closed at {latest.index_close?.toFixed(2) ?? "—"}, against a
            10-day average of {latest.index_ma_fast?.toFixed(2) ?? "—"} and a 20-day average of{" "}
            {latest.index_ma_slow?.toFixed(2) ?? "—"} — {indexWord(latest)}. Breadth decides the
            gate; the index can only make it worse, never better.
          </p>
        </section>
      ) : null}
    </div>
  );
}
