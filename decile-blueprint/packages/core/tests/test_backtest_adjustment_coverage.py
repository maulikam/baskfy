"""What a backtest has to say about the prices it read.

**These tests were rewritten the same day they were written, because the first version asserted a
model of the data that is false.** It assumed one adjusted series whose quality was bounded by how
far back `corporate_action` goes. Measured on the deployed box, `ohlcv_daily` is two spliced
segments:

    source='kite'  2,412,061 rows  2017-01-02 → 2026-09-10   adjusted AT SOURCE
    source='nse'   1,341,173 rows  2024-01-01 → 2026-09-11   adjusted by us

Kite returns adjusted OHLC (M24, measured), so the deep segment carries `adj_factor = 1` and
`reprocess_instrument` skips it by name. Our corporate-action table is irrelevant to those years.
The first version of these tests asserted the engine should warn that "splits before 2024 are not
applied" on a run starting 2017 — and that warning would have been wrong, on the screen, in front
of somebody sizing a position.

What is worth warning about is the **splice**: our segment is price-return adjusted and Kite's is
total-return, so a return measured wholly before the join carries the dividend adjustment and one
measured after does not. On NSE that is roughly 1.2 % a year, compounding — a systematic tilt on a
momentum ranking rather than noise.
"""

from __future__ import annotations

import datetime as dt

from baskfy_core.backtest import AdjustmentCoverage

DEEP_START = dt.date(2017, 1, 2)
SPLICE = dt.date(2024, 1, 1)
FIRST_ACTION = dt.date(2024, 1, 2)
TODAY = dt.date(2026, 9, 11)

#: Exactly what the deployed box looks like, so these tests fail if its shape changes.
BOX = AdjustmentCoverage(
    actions_from=FIRST_ACTION,
    actions_to=TODAY,
    self_adjusted_from=SPLICE,
    splice_on=SPLICE,
)


def test_a_deep_run_is_not_told_its_splits_are_missing() -> None:
    """The regression this file exists for. Those years are adjusted at source."""
    notes = BOX.caveats(DEEP_START, TODAY)
    gap = [n for n in notes if "Splits and bonuses inside it" in n]

    # There is a gap, and it is one day wide — not the seven years the first version implied.
    assert len(gap) == 1
    assert "a gap of 1 day" in gap[0]
    assert "2017" not in gap[0]


def test_a_deep_run_is_told_about_the_splice_instead() -> None:
    """The caveat that is actually true, and the one a momentum ranking is sensitive to."""
    notes = BOX.caveats(DEEP_START, TODAY)
    splice = next(n for n in notes if "different source" in n)

    assert SPLICE.isoformat() in splice
    assert "dividends as well as splits" in splice
    assert "higher than the later convention" in splice


def test_a_run_wholly_after_the_splice_hears_nothing_about_it() -> None:
    """A caveat that fires on every run is furniture. This one has to be earned."""
    notes = BOX.caveats(dt.date(2025, 1, 1), TODAY)

    assert not any("different source" in n for n in notes)
    assert not any("Splits and bonuses inside it" in n for n in notes)


def test_the_gap_names_its_width_so_one_day_reads_differently_from_seven_years() -> None:
    """Crying wolf on a one-day boundary is how a real gap later goes unread."""
    wide = AdjustmentCoverage(
        actions_from=dt.date(2024, 1, 2),
        actions_to=TODAY,
        self_adjusted_from=DEEP_START,  # pretend we adjusted the deep years ourselves
        splice_on=None,
    )
    gap = next(n for n in wide.caveats(DEEP_START, TODAY) if "gap of" in n)
    assert "gap of 2556 days" in gap


def test_no_actions_at_all_is_its_own_sentence() -> None:
    """An empty table and a late-starting one are different failures and read differently."""
    notes = AdjustmentCoverage(
        actions_from=None, actions_to=None, self_adjusted_from=SPLICE
    ).caveats(DEEP_START, TODAY)

    assert any("unadjusted in fact" in n for n in notes)
    assert not any("only from" in n for n in notes)


def test_a_run_that_never_reaches_the_self_adjusted_segment_is_not_warned_about_it() -> None:
    """A backtest ending before our own segment begins does not depend on our table at all."""
    notes = BOX.caveats(DEEP_START, dt.date(2023, 12, 31))
    assert not any("Splits and bonuses inside it" in n for n in notes)


def test_the_as_of_today_convention_is_stated_even_when_everything_else_is_clean() -> None:
    """§21.9's finding: WHEN the adjustment was applied, not how far back the actions go.

    `reprocess_instrument` takes an `as_of` and the nightly deliberately passes nothing, because
    today's screen should be served today's series — that is its documented intent, not a bug.
    §21.9 declined to settle whether to store one adjusted series per as-of date; this does not
    settle it either. It stops the silence.
    """
    clean = AdjustmentCoverage(
        actions_from=dt.date(2016, 1, 1), actions_to=TODAY, self_adjusted_from=dt.date(2016, 1, 1)
    )
    notes = clean.caveats(DEEP_START, TODAY)

    assert len(notes) == 1
    assert "as of the day this ran" in notes[0]


def test_a_point_in_time_reconstruction_with_one_clean_segment_says_nothing_at_all() -> None:
    clean = AdjustmentCoverage(
        actions_from=dt.date(2016, 1, 1),
        actions_to=TODAY,
        self_adjusted_from=dt.date(2016, 1, 1),
        as_of_today=False,
    )
    assert clean.caveats(DEEP_START, TODAY) == ()
