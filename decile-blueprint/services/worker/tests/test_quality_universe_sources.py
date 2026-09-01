"""`universe_sizes` must block a bad night, and must not block a permanent product gap.

On 2026-08-27 the gate refused a run in which every other check passed — 2546 bars against a
10-day median of 2532, factor rows matching bars, no null prices, no unexplained jumps — because
two of the fourteen selectable universes were empty. They had been empty every day since the box
was built, for a reason that has nothing to do with that night: `etf` is derived from
`instrument.instrument_type == "ETF"` and nothing ever writes that type, and `nifty-fno` needs an
NSE constituent file no provider fetches.

So the whole product served a nine-day-old session over a gap that no amount of re-running could
close. These assert the distinction that fixes it — and, more importantly, that the distinction
cannot be widened into a way of ignoring a real failure.
"""

from __future__ import annotations

from baskfy_core.universes import UNIVERSES
from baskfy_worker.tasks.membership import DERIVED_BY_RULE
from baskfy_worker.tasks.quality import NO_MEMBERSHIP_SOURCE


class TestTheExemptionIsNarrowAndStaysNarrow:
    def test_it_names_only_universes_that_exist(self) -> None:
        slugs = {u.slug for u in UNIVERSES}
        assert slugs >= NO_MEMBERSHIP_SOURCE, sorted(NO_MEMBERSHIP_SOURCE - slugs)

    def test_it_is_a_short_list_and_not_a_habit(self) -> None:
        """Two of fourteen. If this ever approaches the size of the catalogue, the gate has been
        turned off one universe at a time rather than fixed."""
        assert len(NO_MEMBERSHIP_SOURCE) <= 3, sorted(NO_MEMBERSHIP_SOURCE)

    def test_the_indices_with_real_constituent_files_are_never_exempt(self) -> None:
        """The headline universes are what the product screens. If nifty-50 comes back empty the
        night IS broken, and the gate must still say so."""
        for slug in ("nifty-50", "nifty-100", "nifty-500", "nifty-total-market", "nifty-allcap"):
            assert slug not in NO_MEMBERSHIP_SOURCE, f"{slug} must never be exempt"

    def test_allcap_is_derived_by_rule_and_still_not_exempt(self) -> None:
        """`nifty-allcap` is derived the same way `etf` is — every EQ instrument with a bar.

        It is not exempt, and that is the point: being rule-derived is not the reason `etf` is
        listed. `etf`'s rule selects a type nothing writes, so it can never be non-empty; allcap's
        rule works and returned 2546 members on the run in question. An empty allcap would mean
        the day genuinely had no bars.
        """
        assert "nifty-allcap" in DERIVED_BY_RULE
        assert "nifty-allcap" not in NO_MEMBERSHIP_SOURCE


class TestTheReasonsAreWrittenDownWhereTheyAreUsed:
    def test_each_exempt_universe_is_explained_in_the_source(self) -> None:
        """A bare set of slugs decays into folklore. The next person to read this needs to know
        what would have to change for the entry to be removed."""
        import inspect  # noqa: PLC0415 - only this test reads source

        from baskfy_worker.tasks import quality  # noqa: PLC0415 - only this test reads source

        source = inspect.getsource(quality)
        head = source[: source.index("async def check_universe_sizes")]
        for slug in NO_MEMBERSHIP_SOURCE:
            assert slug in head, f"{slug} is exempt with no reason recorded beside the constant"

    def test_the_gate_still_reports_them_on_a_passing_run(self) -> None:
        """A known gap that stops being mentioned is a known gap that stops being known."""
        import inspect  # noqa: PLC0415 - only this test reads source

        from baskfy_worker.tasks.quality import check_universe_sizes  # noqa: PLC0415

        source = inspect.getsource(check_universe_sizes)
        assert "no membership source wired for" in source
        assert "unsourced" in source
