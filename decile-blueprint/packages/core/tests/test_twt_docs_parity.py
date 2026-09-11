"""``docs/twt/04-business-rules.md`` is the contract; this test is what makes that true.

Every field of :mod:`baskfy_core.twt.config` is listed below with the value ``04`` states and the
clause that states it, and the list is asserted **both ways**: a field re-valued without a doc edit
is red, and a field added with no entry here is red too. ``04`` §4.4 asks specifically that the two
breadth numbers be pinned literally, and they are — twice, once in the table and once on their own,
because DECISIONS-TW TW0.4's whole point is that a VBT recalibration must not be able to move them.

It is worth restating where a test enforces a document: **when the code and a doc disagree, look for
the commit that changed the value before assuming the code is wrong** (root ``CLAUDE.md``, 9 Sep
2026). A setting somebody deliberately changed, with a commit message saying why, is a later fact
than a paragraph. This test says the two are in step; it does not say the document is the authority
over a decision.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
from decimal import Decimal
from pathlib import Path

import pytest

from baskfy_core.twt.config import (
    DEFAULT_TWT_CONFIG,
    RESEARCH_TICK_INR,
    TICK_INR,
    EntryTiming,
    RankKey,
)

DOC = Path(__file__).resolve().parents[4] / "docs" / "twt" / "04-business-rules.md"

#: ``config path -> (the value ``04`` states, the clause that states it)``. The clause is part of
#: the assertion's value even though nothing compares it: a failure should be readable without
#: opening the document first.
DOCUMENTED: dict[str, tuple[object, str]] = {
    "data.instrument_type": ("EQ", "§1.1"),
    "data.series_allowed": (("EQ", "BE", "BZ"), "§1.1"),
    "data.keep_null_series": (True, "§1.2"),
    "data.etf_universe_slug": ("etf", "§1.3"),
    "data.etf_name_pattern": (r"\bETF\b", "§1.3"),
    "data.etf_symbol_pattern": (r"(BEES|ETF|IETF)$", "§1.3"),
    "data.thin_session_min_share": (0.25, "§2.1"),
    "data.thin_session_window_bars": (41, "§2.1"),
    "data.thin_session_min_periods": (5, "§2.1"),
    "data.rolling_min_share": (0.90, "§2.2"),
    "data.bars_required": (260, "§2.3"),
    "scan.min_close_raw": (Decimal("30.0"), "§3.1 line 1"),
    "scan.month_low_multiple": (Decimal("1.3"), "§3.1 line 2"),
    "scan.month_low_months_back": (3, "§3.1 line 2, calendar months"),
    "scan.tight_band_pct": (Decimal("3.01"), "§3.1 line 3"),
    "scan.tight_weeks": (3, "§3.1 line 3"),
    "scan.min_vol_sma": (Decimal("10000.0"), "§3.1 line 5"),
    "scan.vol_sma_bars": (50, "§3.1 line 5, includes the signal day"),
    "breadth.dma_bars": (200, "§4.1"),
    "breadth.min_pct_above_dma": (40.0, "§4.1"),
    "breadth.pct_decimals": (4, "§4.2, 'four decimal places'"),
    "entry.entry": (EntryTiming.NEXT_OPEN, "§5.1"),
    "entry.entry_min_sessions_out": (5, "§3.4"),
    "entry.min_turnover_inr": (Decimal("50000000"), "§3.5, the shipped floor"),
    "entry.research_min_turnover_inr": (Decimal("20000000"), "§3.5, TW2's goldens only"),
    "entry.turnover_avg_bars": (20, "§3.5"),
    "entry.rank_key": (RankKey.SIGNAL_TURNOVER, "§6.3, DECISIONS-TW TW0.2"),
    "sizing.max_slots": (10, "§6.1"),
    "sizing.max_position_pct": (Decimal("12.5"), "§6.2"),
    "sizing.max_position_vs_turnover": (Decimal("0.01"), "§6.2, a fraction not a percent"),
    "sizing.min_trade_value_inr": (Decimal("10000"), "§6.2"),
    "sizing.max_new_entries_per_session": (3, "§6.3"),
    "sizing.risk_multiplier_first_live": (Decimal("0.5"), "§6.4"),
    "sizing.first_live_entries": (10, "§6.4, entries not sessions"),
    "exits.stop_pct": (Decimal("20.0"), "§7.1"),
    "exits.trail_pct": (Decimal("20.0"), "§7.2"),
    "exits.close_clamp_fraction": (Decimal("0.9999"), "§7.2"),
    "exits.close_clamp_fallback": (Decimal("0.999"), "§7.2"),
    "exits.no_bar_sessions": (5, "§7.5"),
    "exits.gtt_limit_fraction": (Decimal("0.97"), "§10.6, TWT_GTT_LIMIT_FRACTION"),
    "exits.gtt_band_min_pct": (Decimal("0.005"), "§10.7, TWT_STOP_BAND"),
    "exits.gtt_band_max_pct": (Decimal("0.30"), "§10.7, TWT_STOP_BAND"),
    "costs.cost_bps_per_side": (Decimal("25.0"), "§8"),
    "backtest.initial_capital_inr": (Decimal("1000000"), "§12"),
    "backtest.start": (dt.date(2017, 10, 16), "§12"),
    "backtest.is_oos_split": (dt.date(2023, 1, 1), "§12"),
}

#: The numbers ``04`` writes out in its own prose, which the document must therefore still contain.
#: ``pct_decimals`` is excluded because §4.2 spells it as the word "four", and a search for "4"
#: would match anything.
HEADLINE = tuple(
    path
    for path, (value, _) in DOCUMENTED.items()
    if isinstance(value, (int, float, Decimal))
    and not isinstance(value, bool)
    and path != "breadth.pct_decimals"
)


def from_code() -> dict[str, object]:
    out: dict[str, object] = {}
    for group in dataclasses.fields(DEFAULT_TWT_CONFIG):
        sub = getattr(DEFAULT_TWT_CONFIG, group.name)
        for spec in dataclasses.fields(sub):
            out[f"{group.name}.{spec.name}"] = getattr(sub, spec.name)
    return out


def doc_text() -> str:
    """``04``, with the thousands spaces and commas removed so ``50 000 000`` reads as a number."""
    return re.sub(r"(?<=\d)[  ,](?=\d)", "", DOC.read_text(encoding="utf-8"))


def test_the_contract_document_exists() -> None:
    assert DOC.is_file(), f"the numerical contract is missing at {DOC}"


def test_every_field_has_an_entry_and_every_entry_has_a_field() -> None:
    """Both directions. A field added without a doc edit is red; a row here with no field is red."""
    assert set(from_code()) == set(DOCUMENTED)


@pytest.mark.parametrize("path", sorted(DOCUMENTED))
def test_the_field_holds_the_value_the_document_states(path: str) -> None:
    expected, clause = DOCUMENTED[path]
    actual = from_code()[path]
    assert actual == expected, f"{path} is {actual!r}; docs/twt/04 {clause} says {expected!r}"


@pytest.mark.parametrize("path", HEADLINE)
def test_the_document_still_contains_the_number(path: str) -> None:
    """The other half of parity: a value changed in both places is fine, and a value changed here
    while ``04`` still prints the old one is the state that cost two days in September."""
    expected, clause = DOCUMENTED[path]
    rendered = format(expected, "f") if isinstance(expected, Decimal) else str(expected)
    trimmed = rendered.rstrip("0").rstrip(".") if "." in rendered else rendered
    text = doc_text()
    assert rendered in text or trimmed in text, (
        f"docs/twt/04 {clause} no longer prints {rendered} for {path}"
    )


class TestTheBreadthNumbersArePinnedLiterally:
    """``04`` §4.4 asks for exactly this, by name. DECISIONS-TW **TW0.4**: share the arithmetic,
    never the calibration — a VBT recalibration must not be able to move this sleeve's gate."""

    def test_the_window_is_two_hundred_sessions(self) -> None:
        assert DEFAULT_TWT_CONFIG.breadth.dma_bars == 200

    def test_the_gate_is_forty_percent(self) -> None:
        assert DEFAULT_TWT_CONFIG.breadth.min_pct_above_dma == 40.0

    def test_the_document_says_so_in_its_own_words(self) -> None:
        text = doc_text()
        assert "BreadthConfig.dma_bars = 200" in text
        assert "BreadthConfig.min_pct_above_dma = 40.0" in text


class TestTheTicks:
    def test_the_exchange_tick_is_five_paise(self) -> None:
        """Not a setting: it is the exchange's, and it is not a field of ``TwtConfig`` for that
        reason."""
        assert TICK_INR == "0.05"

    def test_the_research_tick_is_one_paisa_and_is_named_as_the_researchs(self) -> None:
        """``research/volume-breakout/vbt/sim.py``'s ``_tick`` floors to a paisa. TW2's goldens
        need it to compare trade for trade; nothing else may read it."""
        assert RESEARCH_TICK_INR == "0.01"
        assert Decimal(RESEARCH_TICK_INR) < Decimal(TICK_INR)


class TestWhatTheDocumentSaysIsDeliberatelyAbsent:
    """``04``'s own "what is deliberately absent" paragraph, as an assertion.

    **A field that exists is a field somebody turns on**, and the four below each lowered the
    result when ``01`` §5 measured them."""

    @pytest.mark.parametrize(
        "field",
        [
            "target_pct",
            "partial_r",
            "partial_frac",
            "max_hold",
            "exit_close_below_sma",
            "exit_close_below_ema",
        ],
    )
    def test_no_field_exists_for_a_measured_and_rejected_exit(self, field: str) -> None:
        assert field not in from_code()
        assert not any(path.endswith(f".{field}") for path in from_code())

    def test_there_is_no_trend_filter_group(self) -> None:
        """``01`` §4: VBT-1 has six trend filters and they are the whole argument for that sleeve;
        here they *hurt* — 12.3 % CAGR with a 50/200-SMA filter against 20.9 % without. Adding them
        would be copying a shape instead of a finding."""
        assert not any(path.startswith("trend.") for path in from_code())
