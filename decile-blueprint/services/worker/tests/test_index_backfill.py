"""Matching Kite's index names to `index_def` (M31).

`index_snapshot_daily` held six weeks. Kite carries 136 NSE indices with history back to 2017, so
the levels are a fetch — but only for indices whose names can be matched, and **a wrong match puts
one index's history under another index's name**. That is worse than a missing chart, because a
missing chart is visibly missing.

So the matcher is asserted rather than trusted, in both directions: it must join the pairs that
really are the same index, and must not join ones that are not.
"""

from __future__ import annotations

import pytest

from baskfy_worker.index_backfill import ABBREVIATIONS, keys, normalise


class TestNormalising:
    @pytest.mark.parametrize(
        ("ours", "theirs"),
        [
            ("Nifty Bank", "NIFTY BANK"),
            ("NIFTY 50", "NIFTY 50"),
            ("Nifty50 Dividend Points", "NIFTY50 DIVIDEND POINTS"),
            ("Nifty MidSmallcap400 50:50", "NIFTY MIDSMALLCAP400 50 50"),
        ],
    )
    def test_case_and_punctuation_are_not_differences(self, ours: str, theirs: str) -> None:
        assert normalise(ours) == normalise(theirs)

    def test_it_interprets_nothing(self) -> None:
        """Only case and punctuation. Two different indices must not normalise together."""
        assert normalise("NIFTY 50") != normalise("NIFTY 500")
        assert normalise("Nifty Midcap 50") != normalise("Nifty Midcap 100")


class TestKeys:
    @pytest.mark.parametrize(
        ("ours", "theirs"),
        [
            ("NIFTY SMALLCAP 250", "NIFTY SMLCAP 250"),
            ("Nifty Infrastructure", "NIFTY INFRA"),
            ("Nifty India Consumption", "NIFTY CONSUMPTION"),
            ("Nifty50 PR 2x Leverage", "NIFTY50 PR 2X LEV"),
        ],
    )
    def test_an_abbreviated_name_meets_its_long_form(self, ours: str, theirs: str) -> None:
        assert keys(ours) & keys(theirs)

    def test_expansion_runs_one_way_only(self) -> None:
        """The first version applied substitutions to both sides.

        `NIFTYINDIACONSUMPTION` then became `NIFTYINDIAINDIACONSUMPTION` and stopped matching
        anything — which is how the first run matched 55 of 136 instead of 64. Returning a set of
        candidate forms and intersecting is what removes the need to know which side is short.
        """
        ours = keys("Nifty India Consumption")
        assert "NIFTYINDIACONSUMPTION" in ours, "the literal form must survive expansion"

    def test_distinct_indices_still_do_not_meet(self) -> None:
        """The property that matters. A false match is worse than no match."""
        assert not keys("NIFTY 50") & keys("NIFTY 500")
        assert not keys("Nifty Midcap 150") & keys("Nifty Smallcap 250")
        assert not keys("NIFTY IT") & keys("NIFTY BANK")

    def test_every_abbreviation_actually_shortens(self) -> None:
        """A substitution whose sides are equal is dead weight pretending to be a rule."""
        for short, long in ABBREVIATIONS:
            assert short != long
            assert len(short) < len(long)
