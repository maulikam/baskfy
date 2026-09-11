#!/usr/bin/env python
"""Load the research's panel into the shapes ``baskfy_core.twt`` expects (TW2).

    cd decile-blueprint
    uv run python ../tools/twt/twt_goldens.py

``research/volume-breakout/data/panel.pkl`` is the study's own price panel — 4,186 instruments x
2,396 sessions, 2017-01-02 -> 2026-09-09, built by ``research/volume-breakout/run_research.py``
from the AWS Phase-A box's ``ohlcv_daily``. It is **1.7 GB and gitignored**; the goldens it feeds
are committed and it is not. Nothing here writes to ``research/`` and nothing here reaches a
network or a database.

**The research package is never imported.** ``panel.pkl`` pickles two research dataclasses
(``vbt.data.Panel`` and ``vbt.scan.Indicators``); a remapping unpickler rebinds them to the plain
carriers below, so loading the file does not execute ``research/volume-breakout/vbt/`` and does not
put it on the import path. Every other module the pickle could name is refused.

This module is the TWT twin of ``tools/vbt/research_panel.py`` and differs from it in one way that
matters: VBT-1 loads the **CSV export** and applies ``04`` §1's universe rules itself, while TWT-1
loads the **pickled panel** the tight-close study actually ran on, because TW2's job is to
reproduce *that study's* numbers rather than to re-derive them from the bars. The two differences
this creates are named in ``docs/twt/DECISIONS-TW.md`` TW2.1 and TW2.2.
"""

from __future__ import annotations

import datetime as dt
import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Final

import numpy as np
import numpy.typing as npt
import polars as pl

from baskfy_core.twt.calendar import SessionCalendar, drop_thin_sessions
from baskfy_core.twt.config import DEFAULT_TWT_CONFIG, TwtConfig
from baskfy_core.twt.indicators import REQUIRED_COLUMNS

#: The repository root, three parents up from ``tools/twt/research_panel.py``.
REPO: Final = Path(__file__).resolve().parents[2]
#: The study's panel. Gitignored (1.7 GB); rebuilt by ``research/volume-breakout/run_research.py``.
PANEL: Final = REPO / "research" / "volume-breakout" / "data" / "panel.pkl"
#: The tight-close study's own answers — the goldens TW2 grades against.
RESULTS: Final = REPO / "research" / "tight-close" / "out"
#: Chartink's export of the scan's own history, the recall measurement's answer key.
CHARTINK: Final = REPO / "research" / "tight-close" / "chartink_backtest.csv"

#: What the panel's matrices are called in the pickle, and what they are called in the frame.
_MATRIX_COLUMNS: Final[tuple[str, ...]] = (
    "open",
    "high",
    "low",
    "close",
    "close_raw",
    "volume",
)

#: Modules the unpickler will resolve for real. Everything else is refused by name, so a pickle
#: that grew a new dependency fails loudly here instead of importing research code silently.
_ALLOWED_MODULES: Final[tuple[str, ...]] = ("numpy", "builtins", "collections", "copyreg")

_Matrix = npt.NDArray[np.float64]


class PanelCarrier:
    """A stand-in for a research dataclass: attributes only, no behaviour, no research import.

    Pickle restores a dataclass by creating the instance and updating its ``__dict__``, so a class
    with the same name and no ``__slots__`` receives every field. The properties the research class
    carried (``n``, ``d``, ``col``) are not restored and are not wanted: this module reads shapes.
    """

    __module__ = "tools.twt.research_panel"


class _Unpickler(pickle.Unpickler):
    def find_class(self, module: str, name: str) -> object:
        """``object`` rather than ``type``: numpy's array reconstructor is a function."""
        root = module.split(".", maxsplit=1)[0]
        if root == "vbt":
            return type(name, (PanelCarrier,), {})
        if root in _ALLOWED_MODULES:
            return super().find_class(module, name)
        raise pickle.UnpicklingError(
            f"panel.pkl names {module}.{name}, which this loader refuses to import. "
            "The pickle holds numpy arrays and two research dataclasses and nothing else; "
            "a new name here means the research changed and TW2 has to be read again."
        )


@dataclass(frozen=True, slots=True)
class ResearchPanel:
    """The study's matrices, rows are instruments and columns are sessions.

    A missing bar is ``NaN`` and is never forward-filled — the same layout
    ``baskfy_core.vbt.backtest.Panel`` uses, and the same one the research used.
    """

    symbols: tuple[str, ...]
    instrument_ids: tuple[int, ...]
    sessions: tuple[dt.date, ...]
    open: _Matrix
    high: _Matrix
    low: _Matrix
    close: _Matrix
    close_raw: _Matrix
    volume: _Matrix
    is_etf: npt.NDArray[np.bool_]

    @property
    def instruments(self) -> int:
        return len(self.symbols)

    @property
    def sessions_count(self) -> int:
        return len(self.sessions)

    @property
    def bars(self) -> int:
        """Cells with a close — the row count of the long frame :func:`to_frame` builds."""
        return int(np.isfinite(self.close).sum())


@dataclass(frozen=True, slots=True)
class LoadedPanel:
    """What the core is handed: the plant's frame shape, plus the calendar and the universe."""

    bars: pl.DataFrame
    calendar: SessionCalendar
    universe: pl.DataFrame
    source: Path
    #: Sessions ``04`` §2.1's rule removed **here**. Expected empty: the research dropped its six
    #: when it built the panel, so a non-empty tuple means the two rules disagree.
    dropped_sessions: tuple[dt.date, ...]


def _open(path: Path) -> IO[bytes]:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} is absent. It is 1.7 GB and gitignored; rebuild it with "
            "`cd research/volume-breakout && python run_research.py`, which reads the CSV export "
            "in `research/volume-breakout/aws/`. Without it the study cannot be re-run — the "
            "committed goldens under packages/core/tests/fixtures/twt/ are what TW2 grades "
            "against, and they do not need it."
        )
    return path.open("rb")


def read_panel(path: Path = PANEL) -> ResearchPanel:
    """The pickle, read without importing the research package. Read-only, always."""
    with _open(path) as handle:
        loaded = _Unpickler(handle).load()
    if not isinstance(loaded, tuple):
        raise TypeError(f"{path} holds {type(loaded)!r}; run_research.py pickles a 3-tuple")
    panel = loaded[0]
    fields = panel.__dict__
    dates = np.asarray(fields["dates"]).astype("datetime64[D]")
    return ResearchPanel(
        symbols=tuple(str(symbol) for symbol in fields["symbols"]),
        instrument_ids=tuple(int(value) for value in fields["instrument_ids"]),
        sessions=tuple(date.item() for date in dates),
        open=np.asarray(fields["open"], dtype=np.float64),
        high=np.asarray(fields["high"], dtype=np.float64),
        low=np.asarray(fields["low"], dtype=np.float64),
        close=np.asarray(fields["close"], dtype=np.float64),
        close_raw=np.asarray(fields["close_raw"], dtype=np.float64),
        volume=np.asarray(fields["volume"], dtype=np.float64),
        is_etf=np.asarray(fields["is_etf"], dtype=bool),
    )


def to_frame(panel: ResearchPanel, *, keep_etf_rows: bool = True) -> pl.DataFrame:
    """The matrices as the long frame ``baskfy_core.twt.indicators`` requires.

    One row per **printed** bar — a cell whose close is ``NaN`` is a session the name did not
    trade, and the plant has no row for it either.

    ``keep_etf_rows`` is the research's own reading and the default: the study kept ETFs as rows
    and excluded them inside the scan (``~is_etf``), which means its **breadth denominator counted
    them**. ``04`` §1.3 drops them from the universe instead, so the plant's denominator is
    smaller. Passing ``False`` measures that difference rather than assuming it away
    (DECISIONS-TW TW2.2).
    """
    keep_rows = np.ones(panel.instruments, dtype=bool) if keep_etf_rows else ~panel.is_etf
    rows, columns = np.nonzero(np.isfinite(panel.close) & keep_rows[:, None])
    identifiers = np.asarray(panel.instrument_ids, dtype=np.int64)
    symbols = np.asarray(panel.symbols, dtype=object)
    sessions = np.asarray(panel.sessions, dtype="datetime64[D]")
    frame = pl.DataFrame(
        {
            "instrument_id": identifiers[rows],
            "symbol": pl.Series(symbols[rows], dtype=pl.String),
            "date": pl.Series(sessions[columns]).cast(pl.Date),
            **{name: getattr(panel, name)[rows, columns] for name in _MATRIX_COLUMNS},
            "is_etf": panel.is_etf[rows],
        }
    )
    missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"the converted frame is missing {missing}")
    return frame.sort(["instrument_id", "date"])


def universe_of(panel: ResearchPanel, *, keep_etf_rows: bool = True) -> pl.DataFrame:
    """``(instrument_id, symbol, is_etf)`` for every name the frame holds."""
    keep = np.ones(panel.instruments, dtype=bool) if keep_etf_rows else ~panel.is_etf
    return pl.DataFrame(
        {
            "instrument_id": np.asarray(panel.instrument_ids, dtype=np.int64)[keep],
            "symbol": pl.Series(np.asarray(panel.symbols, dtype=object)[keep], dtype=pl.String),
            "is_etf": panel.is_etf[keep],
        }
    ).sort("instrument_id")


def load(
    config: TwtConfig = DEFAULT_TWT_CONFIG,
    path: Path = PANEL,
    *,
    keep_etf_rows: bool = True,
) -> LoadedPanel:
    """The study's bars, on ``04`` §2's calendar.

    ``drop_thin_sessions`` is applied even though it is expected to remove nothing: the research
    dropped its six thin sessions when it *built* the panel, so this call is the cross-check that
    the two implementations of ``04`` §2.1 agree — an empty ``dropped_sessions`` is the passing
    answer, and a non-empty one is a finding.
    """
    panel = read_panel(path)
    frame = to_frame(panel, keep_etf_rows=keep_etf_rows)
    kept, calendar = drop_thin_sessions(frame, config)
    return LoadedPanel(
        bars=kept,
        calendar=calendar,
        universe=universe_of(panel, keep_etf_rows=keep_etf_rows),
        source=path,
        dropped_sessions=tuple(calendar.dropped),
    )


if __name__ == "__main__":  # pragma: no cover - an operator's smoke test
    loaded = load()
    print(f"{loaded.source}")
    print(f"  instruments {loaded.universe.height}  sessions {len(loaded.calendar.sessions)}")
    print(f"  bars {loaded.bars.height}  ETFs {int(loaded.universe['is_etf'].sum())}")
    print(f"  thin sessions this rule would still drop: {loaded.dropped_sessions or 'none'}")
