"""The three detectors assert docs/swing/04 §1-§4, on series drawn to the method's shapes."""

from __future__ import annotations

from dataclasses import replace

import polars as pl
import pytest
from swing_fixtures import (
    ep_series,
    flag_series,
    flat_series,
    frame,
    last_date,
    parabolic_series,
)

from baskfy_core.swing.config import (
    DEFAULT_SWING_CONFIG,
    TRADEABLE_SETUPS,
    EpConfig,
    FlagConfig,
    LiquidityConfig,
    Setup,
    SwingConfig,
)
from baskfy_core.swing.indicators import REQUIRED_COLUMNS, with_swing_indicators
from baskfy_core.swing.setups import (
    CANDIDATE_COLUMNS,
    CandidateStatus,
    detect_eps,
    detect_flags,
    detect_parabolic,
    detect_setups,
)


def indicated(
    *series: list[dict[str, object]], config: SwingConfig = DEFAULT_SWING_CONFIG
) -> pl.DataFrame:
    return with_swing_indicators(frame(*series), config)


# --- indicators -------------------------------------------------------------


def test_indicators_require_the_documented_columns() -> None:
    bars = frame(flat_series()).drop("volume")
    with pytest.raises(ValueError, match="volume"):
        with_swing_indicators(bars)
    assert "volume" in REQUIRED_COLUMNS


def test_adr_is_the_mean_high_over_low_in_percent() -> None:
    """§1: ADR% = mean over 20 bars of (high / low - 1) x 100. Flat bars at ±2% give 4.08%."""
    ind = indicated(flat_series())
    adr = ind["adr_pct"].tail(1).item()
    assert adr == pytest.approx((1.02 / 0.98 - 1) * 100, rel=1e-9)


def test_up_streak_counts_consecutive_green_closes() -> None:
    ind = indicated(parabolic_series(up_days=4))
    assert ind["up_streak"].tail(1).item() == 4
    ind_red = indicated(parabolic_series(up_days=4, red_last_day=True))
    assert ind_red["up_streak"].tail(1).item() == 0


def test_rvol_compares_today_to_the_past_only() -> None:
    """§3: relative volume is today over the prior 50 bars — today must not dilute its own base."""
    ind = indicated(ep_series(rvol=5.0))
    assert ind["rvol"].tail(1).item() == pytest.approx(5.0, rel=1e-6)


# --- flag -------------------------------------------------------------------


def test_textbook_flag_is_detected_as_setting_up_with_its_pivot_and_low() -> None:
    out = detect_flags(indicated(flag_series(), flat_series()), last_date())
    assert out["symbol"].to_list() == ["FLAGCO"]
    row = out.row(0, named=True)
    assert row["setup"] == Setup.FLAG.value
    assert row["status"] == CandidateStatus.SETTING_UP.value
    assert row["prior_move_pct"] > DEFAULT_SWING_CONFIG.flag.flagpole_min_gain_pct
    assert row["trigger"] == row["pivot_high"]
    assert row["stop_ref"] < row["close"] < row["trigger"]
    assert 0 < row["score"] <= 100


def test_output_columns_are_the_contract_in_order() -> None:
    out = detect_setups(indicated(flag_series()), last_date())
    assert tuple(out.columns) == CANDIDATE_COLUMNS


def test_flag_needs_a_prior_move() -> None:
    """§2: a base without a 30%+ pole in front of it is a sideways stock, not a flag."""
    out = detect_flags(indicated(flag_series(pole_gain=0.15)), last_date())
    assert out.is_empty()


def test_flag_needs_a_base_of_at_least_two_weeks() -> None:
    out = detect_flags(indicated(flag_series(base_bars=6)), last_date())
    assert out.is_empty()


def test_flag_base_may_not_run_longer_than_the_cap() -> None:
    config = replace(DEFAULT_SWING_CONFIG, flag=replace(FlagConfig(), base_max_bars=30))
    out = detect_flags(indicated(flag_series(base_bars=35)), last_date(), config)
    assert out.is_empty()


def test_flag_needs_tightness() -> None:
    """§2: the last 10 bars must range inside 3 ADRs; a noisy base fails."""
    out = detect_flags(indicated(flag_series(base_noise=12.0)), last_date())
    assert out.is_empty()


def test_flag_needs_volume_to_dry_up() -> None:
    out = detect_flags(indicated(flag_series(dryup=False)), last_date())
    assert out.is_empty()


def test_illiquid_names_never_qualify() -> None:
    """§1: ADR% below the floor, or turnover below ₹5 cr, is out before any pattern is read."""
    strict = replace(
        DEFAULT_SWING_CONFIG, liquidity=replace(LiquidityConfig(), turnover_min_inr=1e12)
    )
    out = detect_flags(indicated(flag_series(), config=strict), last_date(), strict)
    assert out.is_empty()


def test_instruments_without_a_bar_on_the_as_of_date_are_absent() -> None:
    out = detect_flags(indicated(flag_series(n=139), flat_series()), last_date(140))
    assert out.is_empty()


def test_breakout_today_is_reported_with_the_day_high_as_trigger() -> None:
    """§2: a close above yesterday's pivot on 1.5x volume is BREAKOUT_TODAY, trigger = day high."""
    rows = flag_series()
    last = rows[-1]
    prior_pivot = max(float(str(r["high"])) for r in rows[-21:-1])
    breakout_close = prior_pivot * 1.03
    rows[-1] = {
        **last,
        "open": prior_pivot * 0.99,
        "high": breakout_close * 1.01,
        "low": prior_pivot * 0.98,
        "close": breakout_close,
        "volume": 5e6,
    }
    out = detect_flags(indicated(rows), last_date())
    assert out["status"].to_list() == [CandidateStatus.BREAKOUT_TODAY.value]
    assert out["trigger"].item() == pytest.approx(breakout_close * 1.01)


# --- EP ---------------------------------------------------------------------


def test_gap_day_out_of_a_neglected_base_is_an_ep() -> None:
    out = detect_eps(indicated(ep_series(), flat_series()), last_date())
    row = out.row(0, named=True)
    assert out.height == 1
    assert row["setup"] == Setup.EP.value
    assert row["status"] == CandidateStatus.GAP_DAY.value
    assert row["gap_pct"] >= DEFAULT_SWING_CONFIG.ep.min_gap_pct
    assert row["rvol"] >= DEFAULT_SWING_CONFIG.ep.min_rvol
    # §3: trigger is the gap day's high, stop reference its low.
    assert row["trigger"] > row["close"] > row["stop_ref"]
    assert row["locked_upper_circuit"] is False


@pytest.mark.parametrize(
    ("series", "why"),
    [
        (ep_series(gap=0.06), "gap below 10%"),
        (ep_series(rvol=1.5), "volume not abnormal"),
        (ep_series(close_position=0.2), "gap did not hold: close in the bottom of the range"),
        (ep_series(prior_gain=0.6), "already extended before the catalyst"),
    ],
)
def test_ep_rejections(series: list[dict[str, object]], why: str) -> None:
    out = detect_eps(indicated(series), last_date())
    assert out.is_empty(), why


def test_ep_locked_at_upper_circuit_is_flagged_not_dropped() -> None:
    """NSE: a gap that locks at the band is still an EP; the plan must know there was no fill."""
    out = detect_eps(indicated(ep_series(locked=True)), last_date())
    assert out.height == 1
    assert out["locked_upper_circuit"].item() is True


def test_ep_threshold_is_configuration_not_a_literal() -> None:
    loose = replace(DEFAULT_SWING_CONFIG, ep=replace(EpConfig(), min_gap_pct=5.0))
    assert detect_eps(indicated(ep_series(gap=0.06)), last_date(), loose).height == 1


# --- parabolic --------------------------------------------------------------


def test_parabolic_runner_is_detected_and_never_tradeable() -> None:
    out = detect_parabolic(indicated(parabolic_series()), last_date())
    row = out.row(0, named=True)
    assert row["setup"] == Setup.PARABOLIC_SHORT.value
    assert row["status"] == CandidateStatus.RUNNING.value
    assert row["up_streak"] >= DEFAULT_SWING_CONFIG.parabolic.min_up_streak
    assert Setup.PARABOLIC_SHORT not in TRADEABLE_SETUPS


def test_first_red_day_after_the_run_is_exhaustion() -> None:
    out = detect_parabolic(indicated(parabolic_series(red_last_day=True)), last_date())
    assert out["status"].to_list() == [CandidateStatus.EXHAUSTION.value]


def test_a_modest_advance_is_not_parabolic() -> None:
    out = detect_parabolic(indicated(parabolic_series(daily_gain=0.03)), last_date())
    assert out.is_empty()


# --- all three --------------------------------------------------------------


def test_detect_setups_concatenates_and_ranks_within_setup() -> None:
    ind = indicated(
        flag_series(1, "FLAG1"),
        flag_series(5, "FLAG2", base_noise=3.0, seed=7),
        ep_series(),
        parabolic_series(),
        flat_series(),
    )
    out = detect_setups(ind, last_date())
    assert set(out["setup"].to_list()) == {"EP", "FLAG", "PARABOLIC_SHORT"}
    flags = out.filter(pl.col("setup") == "FLAG")
    assert flags["score"].to_list() == sorted(flags["score"].to_list(), reverse=True)
    assert "FLATCO" not in out["symbol"].to_list()
