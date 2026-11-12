"use client";

import { useRouter } from "next/navigation";
import { useMemo, useState, useTransition, type FormEvent } from "react";

import { InstrumentLink } from "@/components/instrument/instrument-link";
import { accessToken } from "@/lib/api/browser";
import { apiOrigin } from "@/lib/api/config";
import { EMPTY_CELL, formatTradeDate } from "@/lib/format";
import { liveStatusLine, quoteTitle, useLiveMarks } from "@/lib/screens/live-marks";
import { cn } from "@/lib/utils";
import { formatShares, type WatchBoard } from "@/lib/watchlist/board-types";
import {
  addErrorMessage,
  boardRows,
  defaultAscending,
  formatMove,
  formatVolumeRatio,
  moveTone,
  normaliseSymbol,
  sortBoard,
  type BoardSortKey,
} from "@/lib/watchlist/board-view";

const COLUMNS: readonly { key: BoardSortKey; label: string; hint: string }[] = [
  { key: "symbol", label: "Stock", hint: "Symbol" },
  { key: "day", label: "Today", hint: "Today's move against the previous close, and the price" },
  { key: "day_volume", label: "Today vol", hint: "Shares traded in the session, against the 20-day average" },
  { key: "week_volume", label: "Week vol", hint: "Shares traded over the last 5 sessions" },
  { key: "month_volume", label: "Month vol", hint: "Shares traded over the last 21 sessions" },
  { key: "week", label: "Week", hint: "Change over the last 5 sessions" },
  { key: "month", label: "Month", hint: "Change over the last 21 sessions" },
  { key: "three_month", label: "3 months", hint: "Change over the last 63 sessions" },
  { key: "doubling", label: "Doubling", hint: "The doubling model's score (rank)" },
  { key: "swing", label: "Swing", hint: "The swing model's score (rank)" },
];

async function authHeaders(): Promise<Record<string, string>> {
  const token = await accessToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function Move({ value }: { value: string | null }) {
  const n = value === null ? null : Number(value);
  return <span className={moveTone(n)}>{formatMove(n)}</span>;
}

function Score({ score, rank }: { score: string | null; rank: number | null }) {
  if (score === null) return <span className="text-muted-foreground">{EMPTY_CELL}</span>;
  return (
    <span className="text-xs">
      {Number(score).toFixed(3)}
      {rank !== null ? <span className="text-muted-foreground"> #{rank}</span> : null}
    </span>
  );
}

/** `/watchlist` (Maulik, 12 Nov 2026): the stocks you watch, with today's and recent moves. Read-only. */
export function WatchBoardPanel({ board }: { board: WatchBoard | null }) {
  const router = useRouter();
  const [refreshing, startRefresh] = useTransition();
  const [key, setKey] = useState<BoardSortKey>("day");
  const [asc, setAsc] = useState(false);
  const [input, setInput] = useState("");
  const [adding, setAdding] = useState(false);
  const [message, setMessage] = useState<{ tone: "error" | "info"; text: string } | null>(null);
  const [removing, setRemoving] = useState<ReadonlySet<string>>(new Set());

  const items = useMemo(
    () => (board?.items ?? []).filter((i) => !removing.has(i.symbol)),
    [board, removing],
  );
  const symbols = useMemo(() => items.map((i) => i.symbol), [items]);
  const live = useLiveMarks(symbols);
  const rows = useMemo(
    () => sortBoard(boardRows(items, live.quotes, live.liveOverlay), key, asc),
    [items, live.quotes, live.liveOverlay, key, asc],
  );

  const sortBy = (k: BoardSortKey) => {
    if (k === key) setAsc(!asc);
    else {
      setKey(k);
      setAsc(defaultAscending(k));
    }
  };

  async function add(e: FormEvent) {
    e.preventDefault();
    const symbol = normaliseSymbol(input);
    if (symbol === null) {
      setMessage({ tone: "error", text: "Type an NSE symbol, like RELIANCE or SUNTV." });
      return;
    }
    if (items.some((i) => i.symbol === symbol)) {
      setMessage({ tone: "info", text: `${symbol} is already on your watchlist.` });
      return;
    }
    setAdding(true);
    setMessage(null);
    try {
      const response = await fetch(`${apiOrigin()}/api/v1/watchlist/instruments`, {
        method: "POST",
        headers: { ...(await authHeaders()), "Content-Type": "application/json" },
        body: JSON.stringify({ symbol }),
      });
      if (response.ok || response.status === 409) {
        setInput("");
        setRemoving((prev) => {
          const next = new Set(prev);
          next.delete(symbol);
          return next;
        });
        setMessage({ tone: "info", text: `Added ${symbol}.` });
        startRefresh(() => router.refresh());
      } else {
        setMessage({ tone: "error", text: addErrorMessage(response.status, symbol) });
      }
    } catch {
      setMessage({ tone: "error", text: `Could not add ${symbol}. Try again.` });
    } finally {
      setAdding(false);
    }
  }

  async function remove(symbol: string) {
    setRemoving((prev) => new Set(prev).add(symbol));
    setMessage(null);
    try {
      const response = await fetch(
        `${apiOrigin()}/api/v1/watchlist/instruments/${encodeURIComponent(symbol)}`,
        { method: "DELETE", headers: await authHeaders() },
      );
      if (!response.ok && response.status !== 404) throw new Error(String(response.status));
      startRefresh(() => router.refresh());
    } catch {
      setRemoving((prev) => {
        const next = new Set(prev);
        next.delete(symbol);
        return next;
      });
      setMessage({ tone: "error", text: `Could not remove ${symbol}. Try again.` });
    }
  }

  return (
    <div className="space-y-4">
      <form onSubmit={(e) => void add(e)} className="flex flex-wrap items-end gap-2" aria-label="Add a stock">
        <label className="space-y-1 text-xs text-muted-foreground">
          <span>Add a stock</span>
          <input
            value={input}
            onChange={(e) => setInput(e.target.value.toUpperCase())}
            placeholder="NSE symbol, e.g. SUNTV"
            autoCapitalize="characters"
            autoComplete="off"
            spellCheck={false}
            data-testid="watch-add-input"
            className="block h-9 w-56 rounded-md border border-border bg-background px-2 text-sm uppercase outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
        </label>
        <button
          type="submit"
          disabled={adding || input.trim() === ""}
          className="h-9 rounded-md border border-border bg-card px-3 text-sm font-medium hover:border-accent hover:text-accent disabled:opacity-60"
        >
          {adding ? "Adding…" : "Add"}
        </button>
        {message ? (
          <span
            role={message.tone === "error" ? "alert" : "status"}
            className={cn("pb-2 text-xs", message.tone === "error" ? "text-destructive" : "text-muted-foreground")}
          >
            {message.text}
          </span>
        ) : null}
      </form>

      {board === null ? (
        <p className="text-sm text-muted-foreground">The watchlist is unavailable right now.</p>
      ) : rows.length === 0 ? (
        <div className="grid place-items-center rounded-xl border border-dashed border-border bg-card/50 px-6 py-14 text-center">
          <p className="max-w-[56ch] text-sm leading-relaxed text-muted-foreground">
            Nothing on your watchlist yet. Type a symbol above and press Add, or use “Add to
            watchlist” on a row of the model explorer (Build → Doubling → Explorer) or Save on a
            stock&rsquo;s page.
          </p>
        </div>
      ) : (
        <>
          <p className="flex items-center gap-1.5 text-xs text-muted-foreground" data-testid="live-status">
            {live.liveOverlay ? (
              <span aria-hidden="true" className="inline-block size-1.5 rounded-full bg-positive" />
            ) : null}
            {live.liveOverlay
              ? `${liveStatusLine(live, board.as_of)} · volumes and week/month changes as of ${formatTradeDate(board.as_of)}`
              : liveStatusLine(live, board.as_of)}
            {refreshing ? " · updating…" : ""}
          </p>
          <div className="overflow-x-auto rounded-md border border-border">
            <table className="min-w-[64rem] border-collapse text-sm" data-testid="watch-board">
              <thead>
                <tr className="border-b border-border/70 text-left text-xs uppercase tracking-wide text-muted-foreground">
                  {COLUMNS.map((c) => (
                    <th
                      key={c.key}
                      scope="col"
                      className="px-3 py-2 font-medium"
                      aria-sort={key === c.key ? (asc ? "ascending" : "descending") : "none"}
                    >
                      <button type="button" title={c.hint} onClick={() => sortBy(c.key)} className="uppercase tracking-wide">
                        {c.label}
                        {key === c.key ? (asc ? " ▲" : " ▼") : ""}
                      </button>
                    </th>
                  ))}
                  <th scope="col" className="px-3 py-2">
                    <span className="sr-only">Remove</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => {
                  const quote = live.liveOverlay ? live.quotes[r.symbol.toUpperCase()] : undefined;
                  const ratio = formatVolumeRatio(r.volume_vs_avg20);
                  return (
                    <tr key={r.symbol} className="border-b border-border/40 tabular-nums" data-testid="watch-row">
                      <td className="px-3 py-1.5">
                        <InstrumentLink symbol={r.symbol} flag={false} />
                        {r.name ? <div className="max-w-[14rem] truncate text-xs text-muted-foreground">{r.name}</div> : null}
                      </td>
                      <td
                        className="px-3 py-1.5"
                        title={quote ? quoteTitle(quote) : `Close of ${formatTradeDate(r.last_date)}`}
                        data-source={r.source}
                      >
                        <span className={cn("font-medium", r.source === "stale" ? "text-muted-foreground" : moveTone(r.dayFraction))}>
                          {formatMove(r.dayFraction)}
                        </span>
                        <div className="text-xs text-muted-foreground">
                          {r.price === null ? EMPTY_CELL : `₹${r.price.toFixed(2)}`}
                          {r.source === "close" ? ` · close ${formatTradeDate(r.last_date)}` : r.source === "live" ? " · live" : " · stale"}
                        </div>
                      </td>
                      <td className="px-3 py-1.5" title={`Session of ${formatTradeDate(r.last_date)}`}>
                        {formatShares(r.day_volume)}
                        {ratio ? (
                          <div
                            className={cn(
                              "text-xs",
                              Number(r.volume_vs_avg20) >= 2 ? "font-medium text-accent" : "text-muted-foreground",
                            )}
                          >
                            {ratio}
                          </div>
                        ) : null}
                      </td>
                      <td className="px-3 py-1.5">{formatShares(r.week_volume)}</td>
                      <td className="px-3 py-1.5">{formatShares(r.month_volume)}</td>
                      <td className="px-3 py-1.5"><Move value={r.week_change_fraction} /></td>
                      <td className="px-3 py-1.5"><Move value={r.month_change_fraction} /></td>
                      <td className="px-3 py-1.5"><Move value={r.three_month_change_fraction} /></td>
                      <td className="px-3 py-1.5"><Score score={r.doubling_score} rank={r.doubling_rank} /></td>
                      <td className="px-3 py-1.5">
                        <Score score={r.swing_score} rank={r.swing_rank} />
                        {r.swing_picked ? <span className="ml-1 text-amber-500" title="One of tonight's swing picks">★</span> : null}
                      </td>
                      <td className="px-3 py-1.5 text-right">
                        <button
                          type="button"
                          onClick={() => void remove(r.symbol)}
                          className="rounded-md border border-border/70 px-2 py-0.5 text-xs text-muted-foreground hover:border-destructive hover:text-destructive"
                          aria-label={`Remove ${r.symbol} from watchlist`}
                        >
                          Remove
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <p className="text-xs text-muted-foreground">
            Information only: nothing here places an order. Baskets you watch are under Portfolio → Watchlist.
          </p>
        </>
      )}
    </div>
  );
}
