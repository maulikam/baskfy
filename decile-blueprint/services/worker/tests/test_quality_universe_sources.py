"""`universe_sizes` must block a bad night, and must not block a permanent product gap.

On 2026-08-27 the gate refused a run in which every other check passed — 2546 bars against a
10-day median of 2532, factor rows matching bars, no null prices, no unexplained jumps — because
two of the fourteen selectable universes were empty. They had been empty every day since the box
was built, for a reason that had nothing to do with that night: `etf` was derived from
`instrument.instrument_type == "ETF"` and nothing ever wrote that type, and `nifty-fno` was said
to need an NSE constituent file no provider fetched.

So the whole product served a nine-day-old session over a gap that no amount of re-running could
close. These assert the distinction that fixes it — and, more importantly, that the distinction
cannot be widened into a way of ignoring a real failure.

**BOTH GAPS ARE NOW CLOSED (9 Sep 2026)**: `nifty-fno` comes from Kite's own NFO dump (216
underlyings, no new credential) and `etf` from NSE's published `eq_etfseclist.csv` (350 symbols).
They remain in `NO_MEMBERSHIP_SOURCE` on purpose — sourced, but still not worth failing a night
over. Emptying that list was tried first and turned a 404 on either file into a night the product
could not publish, which is the same harm this module was written about.
"""

from __future__ import annotations

from baskfy_core.universes import UNIVERSES
from baskfy_providers.kite import KiteProvider
from baskfy_providers.nse import NSEProvider
from baskfy_worker.tasks.membership import DERIVED_BY_RULE, PROVIDER_SOURCED
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


class TestTheTwoUniversesThatUsedToScreenToNothing:
    """Maulik, 9 Sep 2026: "why in screen for this index nifty-fno giving all results empty"
    and then "is there any other index which is having these issues".

    Exactly two of the fifteen screenable universes were empty — `nifty-fno` and `etf`. Every
    other one matched its nominal size on the box (nifty-50 = 50, nifty-500 = 500,
    nifty-allcap = 4,275, and so on). Neither was a bug in the screen: both had NO membership
    source at all, and an empty universe honestly screens to nothing.
    """

    def test_the_tolerated_universes_are_the_two_that_do_not_block_a_night(self) -> None:
        """Both are SOURCED now, so they populate on an ordinary day. They stay tolerated
        because removing them made a transient source failure fail the whole night — and this
        module's docstring records nine days of a stale session from exactly that."""
        assert frozenset({"etf", "nifty-fno"}) == NO_MEMBERSHIP_SOURCE
        assert set(PROVIDER_SOURCED) == NO_MEMBERSHIP_SOURCE, (
            "a tolerated universe with no source is the old bug; a sourced universe that is not "
            "tolerated can fail a night over a 404"
        )

    def test_both_are_wired_to_a_provider_method_that_exists(self) -> None:
        """The mapping is only as good as the methods it names — a typo here would put the
        universe straight back to empty, silently, because `_from_named_method` answers [] for a
        provider that lacks the method."""
        assert PROVIDER_SOURCED == {"nifty-fno": "fno_underlyings", "etf": "etf_symbols"}
        assert callable(KiteProvider.fno_underlyings)
        assert callable(NSEProvider.etf_symbols)

    def test_every_screenable_universe_is_resolvable(self) -> None:
        """The whole set, so a new universe cannot be added to the screen without a way to
        fill it — which is how these two came to exist."""
        unresolvable = [
            u.slug
            for u in UNIVERSES
            if u.slug not in PROVIDER_SOURCED and u.slug not in DERIVED_BY_RULE
        ]
        # Everything left must be served by an NSE constituents file; those are the numbered
        # NIFTY indices, and they were all populated on the box.
        assert all(slug.startswith("nifty-") or slug.startswith("nse-") for slug in unresolvable), (
            f"a universe with no plausible source: {unresolvable}"
        )
