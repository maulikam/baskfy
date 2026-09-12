"""TW9 — the study re-run from the plant's own bars, and the row it leaves behind.

Named `..._job` for the reason `test_vbt_backtest_job.py` records: `packages/core/tests` already
has `test_twt_backtest.py` for the pure engine, pytest collects both trees in one run with no
`__init__.py` between them, and two files sharing a basename are a collection error rather than
two test modules. The core one is the engine's arithmetic; this one is the job around it.

`docs/twt/06` § TW9's acceptance, and `gates/twt-9.md`'s eight:

* **the run is sized against `params.sleeve_inr` and never against `tw_config.sleeve_capital_inr`**
  — asserted by a spy over every ORM statement the run issues, because reading the live capital
  into a backtest is how a research number quietly becomes a claim about the user's own money;
* **two runs of the same date append and edit nothing**, because `03` §9 makes the table
  append-only: the figure that was on the page when the execution flag was considered has to
  survive a recalibration that produces a different one;
* **a run that raises writes `finished_at` and `error` and re-raises** — a failed run that looks
  finished is worse than one that is obviously broken;
* **`stats` carries every key `05` §3's card reads**, including `gate_off_cagr_pct`, the one
  number that justifies the gate and whose cell nothing else fills;
* **`drift` compares against `01` §6 and flags past a CAGR point**, naming both figures;
* **TW2.2's warning is honoured**: the ETF breadth denominator is *measured* on the plant's bars
  rather than inherited as the zero it was on the study's sparse export.

THE PLANTED WINDOW
------------------
A year of flat bars, two names that go tight on the same session and one that never does, and a
tail that makes one winner and one loser. It exists so the chain — the universe query, the bar
loader, the thin-session drop, `with_twt_columns`, `signal_mask` at the shipped ₹5 crore floor,
breadth, the gate vector, the engine, the row — is exercised end to end on numbers a person can
check by hand:

* `TWTCO` fills at 100.00 on 2023-01-02, ratchets to a 121.60 trail off a 152.00 high, and is
  sold there on 2023-01-11 — `+20.99 %` against the 100.25 cost-inclusive basis;
* `LOSECO` fills at 100.00 the same session and breaches its 80.00 disaster stop three sessions
  later — the loser that gives the book a profit factor at all;
* `BGCO` prints every session at 60 and is the reason breadth has a denominator.

The window is deliberately **not** the study's: 2022-01-03 → 2023-01-17 at ₹10 lakh over three
names. Its CAGR is nowhere near `01` §6's 20.92 %, and `TestTheDrift` asserts the flag fires —
which is the check working, not failing. "This was a fixture, not the history" is the explanation
the banner asks for.
"""

from __future__ import annotations

import datetime as dt
import inspect
import sys
from decimal import Decimal
from pathlib import Path
from typing import Final, cast

import polars as pl
import pytest
import sqlalchemy as sa
from helpers import make_instrument, requires_db
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import AppUser, IndexMemberDaily, OhlcvDaily, TwBacktestRun, TwConfig
from baskfy_core.models.base import JsonObject
from baskfy_core.twt.backtest import BacktestParams, gate_vector, panel_from_frame, summarise
from baskfy_core.twt.breadth import breadth_series
from baskfy_core.twt.calendar import drop_thin_sessions
from baskfy_core.twt.config import DEFAULT_TWT_CONFIG, TICK_INR
from baskfy_core.twt.drift import DRIFT_CAGR_POINTS, compare
from baskfy_core.twt.published import PUBLISHED
from baskfy_core.twt.signals import signal_mask, with_twt_columns
from baskfy_worker.tasks import twt_backtest as job
from baskfy_worker.tasks.twt_backtest import (
    SOURCE_PLANT,
    latest_finished,
    run_twt_backtest,
    stats_payload,
    two_books,
)

# The fixture's bar shapes are the pure suite's, so a change to `04` §3 breaks one fixture rather
# than two. Same `sys.path` move `test_vbt_backtest_job.py` makes, for the same reason.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "packages" / "core" / "tests"))
from twt_fixtures import flat_bars, sessions, tight_bars

#: Applied to the classes that need Postgres, **not** to the module. Several of the claims below
#: hold without a database at all — a pure stub proves the failure path, an AST read proves there
#: is no edit path, `stats_payload` is a pure function — and a module-level mark would skip those
#: too, leaving each gate with nothing to say when the shared test database is busy.
DB_BACKED: Final = [requires_db, pytest.mark.db]

#: `packages/core/src/baskfy_core/universes.py`: the seeded `etf` universe, index 14. TW4's
#: `etf_instrument_ids` reads membership of exactly this index.
ETF_INDEX_ID: Final = 14

START: Final = dt.date(2022, 1, 3)
BASE_SESSIONS: Final = 260
SLEEVE: Final = Decimal("1000000")

#: `(open, high, low, close)` for the sessions after the signal. Every bar has a **range**: a bar
#: whose open, high and low are one number is a circuit-locked session and `04` §5.2 refuses to
#: fill on it, which is how the first draft of this fixture produced a signal and no trade.
WINNER_TAIL: Final = (
    (100.0, 102.0, 99.0, 101.0),
    (102.0, 112.0, 101.0, 110.0),
    (111.0, 122.0, 110.0, 120.0),
    (121.0, 132.0, 120.0, 130.0),
    (131.0, 142.0, 130.0, 140.0),
    (141.0, 152.0, 140.0, 150.0),
    (150.0, 151.0, 148.0, 149.0),
    (130.0, 131.0, 110.0, 115.0),
    (115.0, 116.0, 114.0, 115.0),
    (115.0, 116.0, 114.0, 115.0),
    (115.0, 116.0, 114.0, 115.0),
    (115.0, 116.0, 114.0, 115.0),
)

#: The same entry, stopped out three sessions later at the 81.60 trail its own 102.00 high set.
#:
#: The last nine bars **wobble by more than 3.01 %** across their week buckets on purpose. Left
#: flat at 80 the name goes tight again in the middle of January and takes a *third* entry on the
#: run's final session, which is correct behaviour by `04` §3 and a fixture that says something
#: other than what this module means to say. It is worth recording that the rule fired: a name
#: that has fallen and then stopped moving is exactly what this scan is for.
LOSER_TAIL: Final = (
    (100.0, 102.0, 99.0, 101.0),
    (98.0, 99.0, 90.0, 92.0),
    (90.0, 91.0, 78.0, 80.0),
    (80.0, 81.0, 79.0, 80.0),
    (72.0, 73.0, 69.0, 70.0),
    (80.0, 81.0, 79.0, 80.0),
    (80.0, 81.0, 79.0, 80.0),
    (80.0, 81.0, 79.0, 80.0),
    (80.0, 81.0, 79.0, 80.0),
    (75.0, 77.0, 74.0, 76.0),
    (80.0, 81.0, 79.0, 80.0),
    (80.0, 81.0, 79.0, 80.0),
)

FLAT_TAIL: Final = tuple((60.0, 60.5, 59.5, 60.0) for _ in WINNER_TAIL)

DAYS: Final = sessions(BASE_SESSIONS + len(WINNER_TAIL), START)
SIGNAL_ON: Final = DAYS[BASE_SESSIONS - 1]
FIRST: Final = DAYS[0]
LAST: Final = DAYS[-1]

#: The winner's own numbers, worked by hand from `WINNER_TAIL` and `04` §5.3/§7.2: filled at the
#: 100.00 open, 25 bps a side makes the basis 100.25, the 152.00 high trails to
#: `tick_floor(152 x 0.80)` = 121.60, and the 2023-01-11 session's 110.00 low takes it there.
WINNER_RETURN_PCT: Final = 20.99
WINNER_EXIT_PRICE: Final = Decimal("121.60")


def _tail_rows(
    instrument_id: int, symbol: str, tail: tuple[tuple[float, float, float, float], ...]
) -> list[dict[str, object]]:
    return [
        {
            "instrument_id": instrument_id,
            "symbol": symbol,
            "date": DAYS[BASE_SESSIONS + index],
            "open": bar[0],
            "high": bar[1],
            "low": bar[2],
            "close": bar[3],
            "close_raw": bar[3],
            "volume": 2_000_000.0,
            "adj_factor": 1.0,
            "is_etf": False,
        }
        for index, bar in enumerate(tail)
    ]


def planted_bars() -> pl.DataFrame:
    """Three names over 272 sessions: a winner, a loser and the breadth denominator."""
    winner = tight_bars(
        count=BASE_SESSIONS,
        start=START,
        base_close=60.0,
        close=90.0,
        volume=2_000_000.0,
        symbol="TWTCO",
    )
    loser = tight_bars(
        count=BASE_SESSIONS,
        start=START,
        base_close=60.0,
        close=90.0,
        volume=2_000_000.0,
        symbol="LOSECO",
    ).with_columns(pl.lit(2, dtype=pl.Int64).alias("instrument_id"))
    background = flat_bars(
        instrument_id=9_999,
        symbol="BGCO",
        count=BASE_SESSIONS,
        start=START,
        close=60.0,
        volume=2_000_000.0,
    )
    columns = winner.columns
    tail = pl.DataFrame(
        _tail_rows(1, "TWTCO", WINNER_TAIL)
        + _tail_rows(2, "LOSECO", LOSER_TAIL)
        + _tail_rows(9_999, "BGCO", FLAT_TAIL),
        schema_overrides={"instrument_id": pl.Int64, "is_etf": pl.Boolean},
    )
    return pl.concat(
        [winner, loser.select(columns), background.select(columns), tail.select(columns)],
        how="vertical_relaxed",
    ).sort(["instrument_id", "date"])


async def _user(session: AsyncSession) -> int:
    user = AppUser(public_id="tw9-user", email="tw9@example.com")
    session.add(user)
    await session.flush()
    return int(user.id)


async def _seed(session: AsyncSession, bars: pl.DataFrame) -> dict[str, int]:
    """The planted frame as `ohlcv_daily` rows the loader will find."""
    ids: dict[str, int] = {}
    for symbol in sorted(bars["symbol"].unique().to_list()):
        ids[str(symbol)] = await make_instrument(session, str(symbol))
    rows: list[OhlcvDaily] = []
    for record in bars.iter_rows(named=True):
        close = Decimal(str(record["close"]))
        rows.append(
            OhlcvDaily(
                instrument_id=ids[str(record["symbol"])],
                date=record["date"],
                open=Decimal(str(record["open"])),
                high=Decimal(str(record["high"])),
                low=Decimal(str(record["low"])),
                close=close,
                close_raw=close,
                volume=int(record["volume"]),
                volume_raw=int(record["volume"]),
                turnover=(close * Decimal(int(record["volume"]))).quantize(Decimal("0.01")),
                adj_factor=Decimal(1),
                source="nse",
            )
        )
    session.add_all(rows)
    await session.flush()
    return ids


async def _seed_planted(session: AsyncSession) -> dict[str, int]:
    return await _seed(session, planted_bars())


async def _seed_etf(session: AsyncSession) -> int:
    """One ETF that prints every session, and its membership of the seeded `etf` universe.

    The plant's ETFs are exactly what TW2.2 said it could not measure: the research export
    carried 2,786 ETF bars in 3.58 million and the plant carries them daily. One name that prints
    on every session of the window is the smallest fixture that makes the difference non-zero.
    """
    instrument_id = await make_instrument(session, "NIFTYBEES")
    rows: list[OhlcvDaily] = []
    members: list[IndexMemberDaily] = []
    for index, day in enumerate(DAYS):
        # A rising ETF, so it sits **above** its own 200-session average once the window is full
        # and therefore moves the numerator as well as the denominator.
        close = Decimal(str(200.0 + index))
        rows.append(
            OhlcvDaily(
                instrument_id=instrument_id,
                date=day,
                open=close,
                high=close + Decimal("1"),
                low=close - Decimal("1"),
                close=close,
                close_raw=close,
                volume=500_000,
                volume_raw=500_000,
                turnover=(close * Decimal(500_000)).quantize(Decimal("0.01")),
                adj_factor=Decimal(1),
                source="nse",
            )
        )
        members.append(
            IndexMemberDaily(index_id=ETF_INDEX_ID, instrument_id=instrument_id, date=day)
        )
    session.add_all(rows)
    session.add_all(members)
    await session.flush()
    return instrument_id


async def _run(
    session: AsyncSession, user_id: int, *, measure_etf_denominator: bool = False
) -> TwBacktestRun:
    return await run_twt_backtest(
        session,
        user_id=user_id,
        start=FIRST,
        end=LAST,
        sleeve_inr=SLEEVE,
        measure_etf_denominator=measure_etf_denominator,
    )


def _stats(row: TwBacktestRun) -> JsonObject:
    assert row.stats is not None
    return dict(row.stats)


class TestThePlantedWindow:
    pytestmark = DB_BACKED

    async def test_it_reproduces_the_planted_trades_through_the_whole_chain(
        self, session: AsyncSession
    ) -> None:
        """Loader, thin-session drop, detection, breadth, engine, row — on two known trades."""
        user_id = await _user(session)
        await _seed_planted(session)

        stats = _stats(await _run(session, user_id))

        assert stats["trades"] == 2
        assert stats["by_reason"] == {"STOP_HIT": 2}
        assert stats["avg_win_pct"] == f"{WINNER_RETURN_PCT:.2f}"

    async def test_the_params_are_written_on_the_way_in(self, session: AsyncSession) -> None:
        """`03` §9: a run that never finished still says what it was asked for."""
        user_id = await _user(session)
        await _seed_planted(session)

        row = await _run(session, user_id)

        assert row.params["start"] == FIRST.isoformat()
        assert row.params["end"] == LAST.isoformat()
        assert row.params["source"] == SOURCE_PLANT
        # The shipped sleeve, not the study's: the exchange's tick and `04` §3.5's ₹5 crore.
        assert row.params["tick"] == TICK_INR
        assert row.params["min_turnover_inr"] == "50000000"
        config = row.params["config"]
        assert isinstance(config, dict)
        assert config["breadth.min_pct_above_dma"] == 40.0
        assert config["sizing.max_slots"] == 10


class TestTheRunNeverReadsTheSleevesCapital:
    """`gates/twt-9.md` G1, and `03` §9's own sentence about it."""

    pytestmark = DB_BACKED

    async def test_no_statement_of_the_run_touches_tw_config_at_all(
        self, session: AsyncSession
    ) -> None:
        """The spy. Every ORM statement the run issues is recorded and none may name `tw_config`.

        Stronger than checking the sleeve amount came out right: an amount that happens to match
        would pass that, and this fails the moment any code path — now or in three modules' time
        — reaches for the table the live capital lives in.
        """
        user_id = await _user(session)
        await _seed_planted(session)
        await _seed_sleeve_capital(session, user_id, Decimal("99000000"))
        seen: list[str] = []

        @sa.event.listens_for(session.sync_session, "do_orm_execute")
        def _record(state: sa.orm.ORMExecuteState) -> None:
            seen.append(str(state.statement).lower())

        try:
            await _run(session, user_id)
        finally:
            sa.event.remove(session.sync_session, "do_orm_execute", _record)

        assert seen, "the spy recorded nothing, so it is not watching the run"
        offenders = [statement for statement in seen if "tw_config" in statement]
        assert offenders == [], f"the backtest read the live sleeve's settings: {offenders}"

    async def test_a_funded_sleeve_changes_nothing_about_the_run(
        self, session: AsyncSession
    ) -> None:
        """The same claim from the other side: the number on the page does not move when the
        book is funded. A backtest whose history changed the day somebody put money in would not
        be a backtest."""
        user_id = await _user(session)
        await _seed_planted(session)
        unfunded = _stats(await _run(session, user_id))

        await _seed_sleeve_capital(session, user_id, Decimal("2500000"))
        funded = await _run(session, user_id)

        assert _stats(funded) == unfunded
        assert funded.params["sleeve_inr"] == str(SLEEVE)


class TestTheSleeveInrIsTheRunsOwnCapital:
    """G1's pure half: no database, so the claim still has a voice when Postgres is busy."""

    def test_the_sleeve_inr_default_is_the_studys_capital_and_not_a_live_one(self) -> None:
        """Pure: `04` §12's ₹10 lakh is what `01` §6's numbers were produced at, and it is the
        default `BacktestParams` hands the engine. A default that read a funded sleeve would make
        every run a different book from the published one."""
        assert BacktestParams().sleeve_inr == DEFAULT_TWT_CONFIG.backtest.initial_capital_inr
        assert BacktestParams().sleeve_inr == Decimal("1000000")
        assert not hasattr(DEFAULT_TWT_CONFIG.backtest, "sleeve_capital_inr")


async def _seed_sleeve_capital(session: AsyncSession, user_id: int, amount: Decimal) -> None:
    """A funded `tw_config` row, so "the run ignores it" is a measurement rather than a vacuum."""
    session.add(TwConfig(user_id=user_id, sleeve_capital_inr=amount))
    await session.flush()


class TestTheTableIsAppendOnly:
    pytestmark = DB_BACKED

    async def test_two_runs_of_the_same_date_append_and_edit_nothing(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session)
        await _seed_planted(session)

        first = await _run(session, user_id)
        first_id, first_stats, first_started = first.id, first.stats, first.started_at
        second = await _run(session, user_id)

        assert second.id != first_id
        stored = (
            await session.execute(sa.select(TwBacktestRun).where(TwBacktestRun.id == first_id))
        ).scalar_one()
        assert stored.stats == first_stats
        assert stored.started_at == first_started
        assert (
            await session.execute(sa.select(sa.func.count()).select_from(TwBacktestRun))
        ).scalar_one() == 2


class TestAppendOnlyIsStructural:
    """G2's pure half: append-only is not a habit, it is the absence of an edit path."""

    def test_the_module_appends_and_issues_no_update_against_a_stored_run(self) -> None:
        """Pure, and the structural half of the same rule: append-only is not a habit, it is the
        absence of an edit path. A `sa.update` here would be the first one."""
        source = Path(job.__file__).read_text(encoding="utf-8")
        for forbidden in ("sa.update(", "update(TwBacktestRun", "sa.delete(", "session.delete("):
            assert forbidden not in source, f"{forbidden} would edit a stored result"


class TestAFailedRunSaysSoAndRaises:
    pytestmark = DB_BACKED

    async def test_a_run_that_raises_records_its_error_and_leaves_the_last_good_number(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_id = await _user(session)
        await _seed_planted(session)
        good = await _run(session, user_id)

        def explode(*args: object, **kwargs: object) -> pl.DataFrame:
            raise RuntimeError("the plant fell over")

        monkeypatch.setattr(job, "with_twt_columns", explode)
        with pytest.raises(RuntimeError, match="the plant fell over"):
            await _run(session, user_id)

        failed = (
            await session.execute(
                sa.select(TwBacktestRun)
                .where(TwBacktestRun.id != good.id)
                .order_by(TwBacktestRun.id.desc())
                .limit(1)
            )
        ).scalar_one()
        assert failed.stats is None
        assert failed.error is not None and "RuntimeError: the plant fell over" in failed.error
        assert "Traceback" in failed.error
        assert failed.finished_at is not None
        # And the page still reads the good one, not the later failure.
        still_good = await latest_finished(session, user_id=user_id)
        assert still_good is not None and still_good.id == good.id

    async def test_an_empty_plant_raises_rather_than_reporting_a_zero_return(
        self, session: AsyncSession
    ) -> None:
        """No bars is not a zero-return strategy, and a row saying so would be a lie."""
        user_id = await _user(session)

        with pytest.raises(ValueError, match="no bars"):
            await _run(session, user_id)

        row = (await session.execute(sa.select(TwBacktestRun))).scalar_one()
        assert row.stats is None
        assert row.error is not None and "ValueError" in row.error
        assert row.finished_at is not None


class TestTheFailurePathWithoutADatabase:
    """G3's pure half, against a stub session."""

    async def test_the_failure_path_raises_without_a_database(self) -> None:
        """Pure: the same claim against a stub session, so G3 holds without Postgres.

        `finished_at` **and** `error` on the row, and the exception re-raised: a failed run that
        looked finished is worse than one that is obviously broken, and a page cannot tell "still
        running" from "died" unless the two states differ on the row.
        """
        recorded: list[TwBacktestRun] = []

        class _Stub:
            def add(self, row: TwBacktestRun) -> None:
                recorded.append(row)

            async def flush(self) -> None:
                return None

            async def execute(self, *args: object, **kwargs: object) -> object:
                raise RuntimeError("the connection went away")

        with pytest.raises(RuntimeError, match="the connection went away"):
            await run_twt_backtest(cast("AsyncSession", _Stub()), user_id=1)

        assert len(recorded) == 1
        row = recorded[0]
        assert row.finished_at is not None
        assert row.error is not None and "RuntimeError: the connection went away" in row.error
        assert row.stats is None
        # Written on the way in, so even this row says what it was asked for.
        assert row.params["source"] == SOURCE_PLANT


#: Every key `apps/web/src/lib/twt/fetch.ts`'s `TwtBacktestStats` declares and
#: `@/lib/twt/view`'s `backtestFigures` and `gateComparison` read. A name missing from `stats`
#: renders as the reason there is no figure — silent on the page, loud only here.
PAGE_STATS_KEYS: Final = (
    "cagr_pct",
    "max_drawdown_pct",
    "calmar",
    "sharpe",
    "trades",
    "win_rate_pct",
    "profit_factor",
    "avg_hold_sessions",
    "exposure_pct",
    "in_sample_cagr_pct",
    "out_of_sample_cagr_pct",
    "in_sample_window",
    "out_of_sample_window",
    "gate_off_cagr_pct",
    "gate_off_max_drawdown_pct",
    "yearly",
    "equity_curve",
)


class TestTheStatsCarryEveryKeyThePageReads:
    pytestmark = DB_BACKED

    async def test_every_key_the_backtest_card_reads_is_on_the_row(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session)
        await _seed_planted(session)

        stats = _stats(await _run(session, user_id))

        missing = [key for key in PAGE_STATS_KEYS if key not in stats]
        assert missing == [], (
            f"`05` §3's card reads these and the row does not carry them: {missing}"
        )

    async def test_the_gate_off_cagr_is_the_one_number_that_justifies_the_gate(
        self, session: AsyncSession
    ) -> None:
        """`05` §3 gives it a cell that nothing else fills, and `01` §5 is the whole argument:
        17.2 % ungated at -43 % against 20.9 % gated at -24.7 %. A card showing the gated figure
        alone would be showing a decision without its counterfactual."""
        user_id = await _user(session)
        await _seed_planted(session)

        stats = _stats(await _run(session, user_id))

        assert isinstance(stats["gate_off_cagr_pct"], str)
        assert isinstance(stats["gate_off_max_drawdown_pct"], str)
        gate_off_trades = stats["gate_off_trades"]
        assert isinstance(gate_off_trades, int)
        assert stats["gate_off_cagr_pct"] != stats["cagr_pct"] or gate_off_trades >= 1

    async def test_the_stats_are_decimal_strings_and_the_curve_is_money(
        self, session: AsyncSession
    ) -> None:
        """House rule 9 all the way to the database, and `@/lib/twt/numbers`' rule that a rate
        converts and rounds exactly once — on the page, from a decimal string."""
        user_id = await _user(session)
        await _seed_planted(session)

        stats = _stats(await _run(session, user_id))

        assert isinstance(stats["cagr_pct"], str)
        curve = stats["equity_curve"]
        assert isinstance(curve, list) and len(curve) > 1
        assert isinstance(curve[0]["equity_inr"], str)
        assert isinstance(stats["yearly"], list)
        assert isinstance(stats["yearly"][0]["return_pct"], str)

    async def test_a_missing_number_is_an_absent_key_and_never_a_json_null(
        self, session: AsyncSession
    ) -> None:
        """`05` §3's `Figure` recognises "no value" as `undefined`; a null would reach `percent()`
        and be printed as the word. An absent key is the honest encoding."""
        user_id = await _user(session)
        await _seed_planted(session)

        stats = _stats(await _run(session, user_id))

        nulls = [key for key, value in stats.items() if value is None]
        assert nulls == [], f"these would render as the word null on the page: {nulls}"


class TestTheStatsShapeIsPure:
    """G6's pure half: the key contract holds wherever it is checked."""

    def test_the_stats_shape_is_a_pure_function_of_the_books(self) -> None:
        """Pure: `stats_payload` over the same planted window without a database, so the key
        contract holds wherever it is checked."""
        clean, calendar = drop_thin_sessions(planted_bars(), DEFAULT_TWT_CONFIG)
        detected = with_twt_columns(clean, calendar, DEFAULT_TWT_CONFIG)
        tagged = detected.with_columns(signal_mask(detected, DEFAULT_TWT_CONFIG).alias("signal"))
        params = BacktestParams(sleeve_inr=SLEEVE, start=FIRST, end=LAST)
        books = two_books(tagged, breadth_series(detected, DEFAULT_TWT_CONFIG), params)
        primary = summarise(books.full)
        assert primary is not None

        payload = stats_payload(books, primary, DEFAULT_TWT_CONFIG, {})

        assert [key for key in PAGE_STATS_KEYS if key not in payload] == []
        # And the panel the gate vector was built from is the one the engine walked.
        #
        # `or True` was appended to this until 12 Sep 2026, which made it always pass — so the
        # comment above described an assertion that was not being made. The property is real and
        # it holds: the gate vector must cover exactly the sessions the engine walked, or the
        # breadth gate is answering about a different stretch of tape than the book traded.
        assert gate_vector(
            breadth_series(detected, DEFAULT_TWT_CONFIG),
            panel_from_frame(tagged, "signal").sessions,
        ).size == len(books.full.sessions)


class TestTheEtfDenominatorIsMeasuredAndNotInherited:
    """`gates/twt-9.md` G7 — DECISIONS-TW **TW2.2**'s warning, honoured.

    TW2 measured the ETF contribution to the breadth denominator at **zero** and said in the same
    entry that the zero is a property of the study's sparse export, which carries 2,786 ETF bars
    in 3.58 million, and **not** of the plant, where they print daily. This run must not inherit
    the assumption, so it measures it.
    """

    pytestmark = DB_BACKED

    async def test_the_run_records_what_counting_etfs_would_have_done(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session)
        await _seed_planted(session)
        await _seed_etf(session)

        stats = _stats(await _run(session, user_id, measure_etf_denominator=True))
        measured = stats["etf_denominator"]

        assert isinstance(measured, dict)
        assert measured["etf_instruments"] == 1
        assert measured["etf_bars"] == len(DAYS)
        assert measured["sessions_compared"] > 0
        # Non-zero on the plant, where TW2 measured zero on the export. The number itself is the
        # finding; what is asserted is that it was *measured* rather than assumed.
        assert measured["max_pct_above_dma_delta"] is not None
        assert measured["gate_verdicts_changed"] is not None

    async def test_an_etf_never_reaches_the_book_itself(self, session: AsyncSession) -> None:
        """`04` §1.3 keeps ETFs out of the universe, and the counterfactual must not change that:
        the denominator question is about breadth alone. The traded universe is the three planted
        names and nothing else."""
        user_id = await _user(session)
        await _seed_planted(session)
        await _seed_etf(session)

        stats = _stats(await _run(session, user_id, measure_etf_denominator=True))

        assert stats["universe"] == 3
        assert stats["trades"] == 2

    async def test_a_plant_with_no_etfs_says_so_rather_than_reporting_a_zero(
        self, session: AsyncSession
    ) -> None:
        """Zero ETF instruments and "counting them changes nothing" are different answers, and a
        page that could not tell them apart is how TW2.2's warning gets lost a second time."""
        user_id = await _user(session)
        await _seed_planted(session)

        stats = _stats(await _run(session, user_id, measure_etf_denominator=True))
        measured = stats["etf_denominator"]

        assert isinstance(measured, dict)
        assert measured["etf_instruments"] == 0
        assert measured["max_pct_above_dma_delta"] is None


class TestTheEtfCounterfactualIsNeverOptIn:
    """G7's pure half."""

    def test_the_etf_counterfactual_is_on_by_default(self) -> None:
        """Pure: an opt-out, never an opt-in. A measurement nobody remembers to ask for is the
        assumption TW2.2 warned about, wearing a keyword."""
        signature = inspect.signature(run_twt_backtest)
        assert signature.parameters["measure_etf_denominator"].default is True


class TestTheDrift:
    pytestmark = DB_BACKED

    async def test_the_planted_window_is_flagged_because_it_is_not_the_study(
        self, session: AsyncSession
    ) -> None:
        """Three names over a year is nowhere near 20.92 % a year on 164 trades, and the flag
        says so. That is the check working: `05` §3's banner asks a person to explain a
        difference, and "this was a fixture, not the history" is the explanation."""
        user_id = await _user(session)
        await _seed_planted(session)

        row = await _run(session, user_id)

        assert row.drift is not None
        assert row.drift["flagged"] is True
        assert row.drift["threshold_cagr_points"] == str(DRIFT_CAGR_POINTS)
        assert row.drift["trades_delta"] == 2 - PUBLISHED.trades
        # Both numbers on the row, because `05` §3's warning names both.
        assert row.drift["published_cagr_pct"] == "20.92"
        assert row.stats is not None
        assert row.drift["run_cagr_pct"] == row.stats["cagr_pct"]


class TestTheDriftArithmetic:
    """The threshold, both directions, and the JSON shape — pure, `01` §6 alone."""

    def test_a_run_that_matches_the_study_is_not_flagged(self) -> None:
        exact = compare(
            cagr_pct=PUBLISHED.cagr_pct,
            max_drawdown_pct=PUBLISHED.max_drawdown_pct,
            trades=PUBLISHED.trades,
        )
        assert exact.flagged is False
        assert (exact.cagr_pct_delta, exact.trades_delta) == (Decimal("0.00"), 0)

    def test_a_point_either_way_is_inside_the_threshold_and_more_is_not(self) -> None:
        """Both directions: beating the study by two points is as suspicious as missing by two."""
        for offset, flagged in ((1.0, False), (-1.0, False), (1.5, True), (-1.5, True)):
            drift = compare(
                cagr_pct=PUBLISHED.cagr_pct + offset, max_drawdown_pct=-24.7, trades=164
            )
            assert drift.flagged is flagged, offset

    def test_the_drift_json_is_the_shape_the_card_reads(self) -> None:
        payload = compare(cagr_pct=16.6, max_drawdown_pct=-30.0, trades=150).to_json()
        assert payload["run_cagr_pct"] == "16.60"
        assert payload["published_cagr_pct"] == "20.92"
        assert payload["cagr_pct_delta"] == "-4.32"
        assert payload["flagged"] is True


class TestThePageReadsTheLatestFinished:
    pytestmark = DB_BACKED

    async def test_a_run_still_in_flight_never_displaces_the_last_good_one(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session)
        await _seed_planted(session)
        good = await _run(session, user_id)
        session.add(
            TwBacktestRun(
                user_id=user_id,
                source=SOURCE_PLANT,
                params={"source": SOURCE_PLANT},
                started_at=dt.datetime.now(tz=dt.UTC) + dt.timedelta(minutes=5),
            )
        )
        await session.flush()

        latest = await latest_finished(session, user_id=user_id)

        assert latest is not None and latest.id == good.id

    async def test_a_later_failed_run_never_displaces_the_last_good_one(
        self, session: AsyncSession
    ) -> None:
        """A failure has a `finished_at` — that is `03` §9's point — so "finished" alone is not
        the query. It is finished **and** carrying stats, which is the same pair the page's own
        `latestFinished` filters on."""
        user_id = await _user(session)
        await _seed_planted(session)
        good = await _run(session, user_id)
        started = dt.datetime.now(tz=dt.UTC) + dt.timedelta(minutes=5)
        session.add(
            TwBacktestRun(
                user_id=user_id,
                source=SOURCE_PLANT,
                params={"source": SOURCE_PLANT},
                started_at=started,
                finished_at=started + dt.timedelta(seconds=1),
                error="RuntimeError: the plant fell over",
            )
        )
        await session.flush()

        latest = await latest_finished(session, user_id=user_id)

        assert latest is not None and latest.id == good.id

    async def test_another_users_run_is_not_this_ones(self, session: AsyncSession) -> None:
        user_id = await _user(session)
        other = AppUser(public_id="tw9-other", email="tw9-other@example.com")
        session.add(other)
        await session.flush()
        await _seed_planted(session)
        await _run(session, user_id)

        assert (await latest_finished(session, user_id=int(other.id))) is None

    async def test_the_research_source_is_a_different_question_from_the_plants(
        self, session: AsyncSession
    ) -> None:
        """`05` §3: never mixed, never averaged. A PLANT run does not answer for the export."""
        user_id = await _user(session)
        await _seed_planted(session)
        await _run(session, user_id)

        assert (await latest_finished(session, user_id=user_id, source="RESEARCH_EXPORT")) is None
