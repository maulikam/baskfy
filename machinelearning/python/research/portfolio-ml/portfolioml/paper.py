"""PM8: the paper book's rules, as the research ran them (PM0.8 book of record, PM0.15–0.17 trigger lines,
PM0.13 overlay). Arrays and frames in, numbers out; nothing here reads a database or places anything."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from portfolioml import census as C
from portfolioml import overlay as O
from portfolioml.timing import CASH_RATE, COST

HORIZON, TOP_N, SLICES = 250, 20, 12
WATCH, WINDOW, MAX_LINES, HIGH = 50, 21, 20, 63
TRANCHE = 1 / SLICES
SLOTS = {"book": TRANCHE / TOP_N, "hi63_20": TRANCHE / 20, "hi63_16": TRANCHE / 16}
OVERLAY_DMA = 100
# what the research says to expect (KICKOFF PM8; STRATEGY §4, PM0.17)
EXPECT = {"touched_2x": (0.20, 0.23), "halved": 0.07, "median_12m": 0.06, "hi63_20_max_dd": -0.26}


def month_starts(sessions: list) -> list:
    return [d for i, d in enumerate(sessions) if i == 0 or (d.year, d.month) != (sessions[i - 1].year, sessions[i - 1].month)]


def book_picks(ranked_investable: list[int], recent: list[list[int]], n: int = TOP_N) -> list[int]:
    """Top ``n`` investable names by rank, skipping any name in the last SLICES tranches (skip-held)."""
    held = {i for t in recent[-SLICES:] for i in t}
    return [i for i in ranked_investable if i not in held][:n]


@dataclass(frozen=True)
class Fired:
    sig: int  # tranche signal session index
    iid: int
    e: int  # session index the 63-day high fired; entry at e + 1
    rank: int


def triggers(watch: dict[int, list[tuple[int, int]]], flag, through: int, already: list[Fired], admit=None) -> list[Fired]:
    """New trigger lines up to session ``through``: each watched name's first 63-day high in (sig, sig+WINDOW],
    at most MAX_LINES a tranche in (session, rank) order, never while the same name's earlier line is held.
    ``flag(iid, t)`` says whether the name closed at a 63-session high on session t. ``admit(iid, e)``: the backtest
    seed's filter (a completed outcome, as the research had); live lines take every firing."""
    last = {}
    for f in sorted(already, key=lambda f: (f.sig, f.e, f.rank)):
        last[f.iid] = max(last.get(f.iid, -1), f.e + HORIZON)
    have = {(f.sig, f.iid) for f in already}
    count = {}
    for f in already:
        count[f.sig] = count.get(f.sig, 0) + 1
    new = []
    for sig in sorted(watch):
        cands = []
        for iid, rk in watch[sig]:
            if (sig, iid) in have:
                continue
            e = next((t for t in range(sig + 1, min(sig + WINDOW, through) + 1) if flag(iid, t)), None)
            if e is not None:
                cands.append(Fired(sig, iid, e, rk))
        for f in sorted(cands, key=lambda f: (f.e, f.rank)):
            if count.get(sig, 0) >= MAX_LINES or last.get(f.iid, -1) >= f.e or (admit is not None and not admit(f.iid, f.e)):
                continue
            new.append(f)
            count[sig] = count.get(sig, 0) + 1
            last[f.iid] = f.e + HORIZON
    return new


def line_path(closes: np.ndarray, start: int, entry: float, last_bar: int, write_off: bool) -> np.ndarray:
    """Multiples of entry from session ``start`` for up to HORIZON sessions, cut at the end of the data (census.paths, variant b)."""
    end = min(start + HORIZON, closes.shape[0])
    return C.paths(closes[start:end, None], np.array([entry]), np.array([last_bar - (start - 1)]), np.array([write_off]), "b")[:, 0]


def pool_marks(lines: list[tuple[int, np.ndarray, float]], n_sessions: int, first: int) -> tuple[np.ndarray, np.ndarray]:
    """timing.pool_book_detail with an open end: a line whose path stops at the data's last session is still held,
    not sold. Returns daily equity and invested share from session ``first``."""
    daily = (1 + CASH_RATE) ** (1 / 252)
    by_start: dict[int, list[tuple[np.ndarray, float]]] = {}
    for s, p, w in lines:
        by_start.setdefault(s, []).append((p, w))
    cash, active = 1.0, []
    out, invested = np.ones(n_sessions - first), np.zeros(n_sessions - first)
    for t in range(first, n_sessions):
        cash *= daily
        starts = by_start.get(t, [])
        if starts:
            prior = sum(u * p[t - 1 - s] for u, p, s in active if t - 1 >= s)
            equity = cash + prior
            want = np.array([w * equity for _, w in starts])
            scale = min(1.0, cash / want.sum()) if want.sum() > 0 else 0.0
            for (p, _), amount in zip(starts, want * scale):
                cash -= amount
                active.append((amount * (1 - COST), p, t))
        still = []
        for u, p, s in active:
            if t - s == HORIZON - 1:  # a full holding period: sold at this close
                cash += u * p[-1] * (1 - COST)
            else:
                still.append((u, p, s))
        active = still
        value = sum(u * p[min(t - s, len(p) - 1)] for u, p, s in active)
        out[t - first] = cash + value
        invested[t - first] = value / out[t - first]
    return out, invested


def overlay_marks(book: np.ndarray, index_close: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """PM0.13's close > 100-DMA held 5 sessions, traded a session late, cash at CASH_RATE, O.COST a switch,
    applied to the book's daily return. ``index_close`` is aligned to ``book``'s sessions (NaN-free)."""
    sma = np.convolve(index_close, np.ones(OVERLAY_DMA) / OVERLAY_DMA, mode="full")[: len(index_close)]
    raw = np.where(np.arange(len(index_close)) >= OVERLAY_DMA - 1, index_close > sma, True)
    held = O.positions(O.debounce(raw))
    daily = (1 + O.CASH_RATE) ** (1 / 252)
    v = np.ones(len(book))
    for t in range(1, len(book)):
        v[t] = v[t - 1] * (book[t] / book[t - 1] if held[t] else daily)
        if held[t] != held[t - 1]:
            v[t] *= 1 - O.COST
    return v, held.astype(float)


class Grid:
    """Bars on the session grid: closes and highs (sessions × names), each name's last bar, write-offs, next-bar opens."""

    def __init__(self, bars, written_off: set[int]) -> None:
        import polars as pl
        bars = bars.sort("instrument_id", "date")
        self.sessions = bars["date"].unique().sort().to_list()
        self.sidx = {d: i for i, d in enumerate(self.sessions)}
        self.ids = {int(v): k for k, v in enumerate(bars["instrument_id"].unique().sort().to_list())}
        rows = [self.sidx[d] for d in bars["date"].to_list()]
        cols = [self.ids[int(i)] for i in bars["instrument_id"].to_list()]
        self.closes = np.full((len(self.sessions), len(self.ids)), np.nan)
        self.highs = np.full_like(self.closes, np.nan)
        self.closes[rows, cols] = bars["close"].to_numpy()
        self.highs[rows, cols] = bars["high"].to_numpy()
        self.last = np.where(~np.isnan(self.closes), np.arange(len(self.sessions))[:, None], -1).max(axis=0)
        self.writes = {i: i in written_off for i in self.ids}
        nxt = bars.with_columns(pl.col("open").shift(-1).over("instrument_id").alias("next_open"),
                                pl.col("date").shift(-1).over("instrument_id").alias("next_date"))
        self.entry = {(int(i), self.sidx[d]): (px, nd) for i, d, px, nd in nxt.select("instrument_id", "date", "next_open", "next_date").iter_rows() if px is not None}
        hi = bars.with_columns((pl.col("close") >= pl.col("close").rolling_max(HIGH, min_samples=HIGH).over("instrument_id")).fill_null(False).alias("hi"))
        self.fired = {(int(i), self.sidx[d]) for i, d in hi.filter(pl.col("hi")).select("instrument_id", "date").iter_rows()}

    def path(self, iid: int, signal: int) -> tuple[int, np.ndarray, float] | None:
        """The line entered at the next open after session ``signal``: (start, multiples, entry price)."""
        got = self.entry.get((iid, signal))
        if got is None or iid not in self.ids:
            return None
        k, start = self.ids[iid], signal + 1
        return start, line_path(self.closes[:, k], start, got[0], int(self.last[k]), self.writes[iid]), got[0]

    def max_mult(self, iid: int, start: int, entry: float) -> float:
        h = self.highs[start:min(start + HORIZON, len(self.sessions)), self.ids[iid]]
        return float(np.nanmax(h) / entry) if np.isfinite(h).any() else float("nan")
