"""OP14 — the day it goes wrong, it goes wrong loudly and safely (`docs/options/06` OP14).

* **token death with a position open → immediate `HARD_EXIT / FEED_LOST`**: at 11:00 an O1 position
  would otherwise wait for `04` §8.5's 14:00 feed watch; a refused token raises its exit on the
  first failed poll (`TestTokenDeath`). A network error does not: a quiet tape is not a dead token.
* restart resume, stale quotes and feed loss are OP9's (`test_options_monitor.py::TestTheRestart`,
  `TestTheReplays` — the `o1-feed-lost` fixture — and `TestTheLoop`), re-run whole with this suite.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_options_monitor import Bus, _fixture, replay  # noqa: E402 - the sibling harness

from app.options_clock import LegQuotes, is_token_error, run_session  # noqa: E402
from app.strategies.nifty_options import NiftyOptionsMonitor  # noqa: E402

DAY = dt.date(2026, 10, 27)


class TokenException(Exception):  # noqa: N818 - kiteconnect's own name, matched by name too
    """Stands for `kiteconnect.exceptions.TokenException`."""


class DeadKite:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls = 0

    def quote_raw(self, keys: list[str]) -> dict:
        self.calls += 1
        raise self.exc


class Ticking:
    """A monotonic clock that moves ten seconds per read, so every pass is a quiet one."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        self.t += 10.0
        return self.t


def _run(exc: Exception) -> list[tuple[str, str, dt.datetime]]:
    fixture = _fixture("o1-profit")
    store = replay.ListStore([replay.position_of(fixture["position"])], [])
    monitor = NiftyOptionsMonitor(None, store=store, day=DAY)
    clock = Ticking()
    moments = iter([dt.datetime(2026, 10, 27, 11, 0, s) for s in range(0, 20, 2)]
                   + [dt.datetime(2026, 10, 27, 15, 31)] * 4)  # fmt: skip
    quotes = LegQuotes(DeadKite(exc), min_interval=5.0, clock=clock)
    asyncio.run(run_session(
        monitor, Bus(), quotes=quotes, now=lambda: next(moments),
        sleep=lambda _s: asyncio.sleep(0), clock=clock,
    ))  # fmt: skip
    return [(code, reason, at) for _, code, reason, at in store.exits]


class TestTokenDeath:
    def test_a_refused_token_raises_every_exit_at_once(self) -> None:
        exits = _run(TokenException("Incorrect `api_key` or `access_token`."))
        assert [(c, r) for c, r, _ in exits] == [("HARD_EXIT", "FEED_LOST")]
        assert exits[0][2] < dt.datetime(2026, 10, 27, 11, 1)  # not 14:00: at once

    def test_a_network_error_is_not_a_dead_token(self) -> None:
        exits = _run(RuntimeError("connection reset"))
        # Nothing at 11:00; the script's last moment (15:31) is past the hard exit, which fires.
        assert [e for e in exits if e[2] < dt.datetime(2026, 10, 27, 14, 0)] == []

    def test_the_real_kiteconnect_exception_is_recognised(self) -> None:
        from kiteconnect.exceptions import TokenException as Real  # noqa: PLC0415

        assert is_token_error(Real("token expired"))
        assert not is_token_error(RuntimeError("token expired"))

    def test_a_successful_poll_clears_the_state(self) -> None:
        class Flaky:
            def __init__(self) -> None:
                self.fail = True

            def quote_raw(self, keys: list[str]) -> dict:
                if self.fail:
                    raise TokenException("expired")
                return {}

        kite = Flaky()
        clock = Ticking()
        quotes = LegQuotes(kite, clock=clock)
        quotes.poll([9001], dt.datetime(2026, 10, 27, 11, 0))
        assert quotes.token_dead
        kite.fail = False
        quotes.poll([9001], dt.datetime(2026, 10, 27, 11, 1))
        assert not quotes.token_dead
