"""What a backtest has to say about the prices it read.

**A momentum screen is the case that makes this matter.** `ohlcv_daily.close` is adjusted only for
the corporate actions on record, and `docs/DECISIONS.md` §21.8 measured how few there are: NSE's
endpoint serves a recent window and ignores how far back it is asked. On the deployed box on
11 Sep 2026 the bars begin 2017-01-02 and the corporate actions begin 2024-01-02 — seven years
with no coverage.

A twelve-month momentum factor whose window spans an unapplied 1:2 split reads as a 50 % loss, and
the name ranks at the bottom of a decile it belongs near the top of. The backtest is then scoring
price breaks rather than returns, and it says so nowhere: it prints a confident CAGR with nothing
attached.

These tests are the spec for saying so. They assert the SENTENCES, because the sentence is the
deliverable — a coverage window stored and never rendered would help nobody.
"""

from __future__ import annotations

import datetime as dt

from baskfy_core.backtest import AdjustmentCoverage

RUN_START = dt.date(2017, 1, 2)
RUN_END = dt.date(2026, 9, 11)


def test_a_run_reaching_before_the_actions_says_so_and_names_both_dates() -> None:
    coverage = AdjustmentCoverage(
        actions_from=dt.date(2024, 1, 2), actions_to=dt.date(2026, 9, 11)
    )
    notes = coverage.caveats(RUN_START, RUN_END)

    gap = next(n for n in notes if "only from" in n)
    # Both dates, because "some history is unadjusted" is not actionable and "before 2024-01-02,
    # and you started 2017-01-02" is.
    assert "2024-01-02" in gap
    assert "2017-01-02" in gap
    assert "wrong by its ratio" in gap


def test_a_run_entirely_inside_the_coverage_raises_no_gap_note() -> None:
    """The caveat must not be boilerplate. A run that IS fully adjusted says nothing about it."""
    coverage = AdjustmentCoverage(
        actions_from=dt.date(2016, 1, 1), actions_to=RUN_END, as_of_today=False
    )
    assert coverage.caveats(RUN_START, RUN_END) == ()


def test_starting_exactly_on_the_first_action_is_covered() -> None:
    """The boundary, because off-by-one here would cry wolf on every fully-covered run."""
    coverage = AdjustmentCoverage(
        actions_from=RUN_START, actions_to=RUN_END, as_of_today=False
    )
    assert coverage.caveats(RUN_START, RUN_END) == ()


def test_no_actions_at_all_is_its_own_sentence_not_a_date_comparison() -> None:
    """An empty table and a late-starting one are different failures and read differently."""
    notes = AdjustmentCoverage(actions_from=None, actions_to=None).caveats(RUN_START, RUN_END)
    assert any("unadjusted throughout" in n for n in notes)
    assert not any("only from" in n for n in notes)


def test_the_as_of_today_convention_is_stated_even_when_coverage_is_complete() -> None:
    """§21.9's finding, which is about WHEN the adjustment was applied, not how far back it goes.

    `reprocess_instrument` takes an `as_of` and the nightly deliberately passes nothing, because
    today's screen should be served today's series — that is its documented intent, not a bug. The
    consequence for a BACKTEST is a different matter: it reads a series carrying actions whose
    ex-date falls after the day being simulated. §21.9 declined to settle whether to store one
    adjusted series per as-of date, and this does not settle it either. It stops the silence.
    """
    coverage = AdjustmentCoverage(actions_from=dt.date(2016, 1, 1), actions_to=RUN_END)
    notes = coverage.caveats(RUN_START, RUN_END)

    assert len(notes) == 1
    assert "as of the day this ran" in notes[0]
    assert "predate it" in notes[0]


def test_a_point_in_time_reconstruction_carries_no_as_of_caveat() -> None:
    """The flag is what a caller that DID reconstruct point-in-time series turns off."""
    coverage = AdjustmentCoverage(
        actions_from=dt.date(2016, 1, 1), actions_to=RUN_END, as_of_today=False
    )
    assert coverage.caveats(RUN_START, RUN_END) == ()
