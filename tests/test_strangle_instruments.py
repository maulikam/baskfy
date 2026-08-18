"""Three underlyings on one account.

The failures pinned here are the ones that do not announce themselves. A calendar built on
the wrong weekday still returns a calendar; a session started with NIFTY's index token still
gets candles; three configs each sized to 40% of the same account each report a comfortable
39%. Every one of them produces a plausible number that is wrong.
"""
from __future__ import annotations

import ast
import datetime as dt
import pathlib

import pytest
import yaml

from app.analytics import autorun as AR
from app.strategies.strangle import allocation as ALLOC
from app.strategies.strangle import calendar_nse as CALN
from app.strategies.strangle import clock as C
from app.strategies.strangle import instruments as INS

CONFIGS = {u.slug: yaml.safe_load(open(u.config))
           for u in INS.REGISTRY.values() if pathlib.Path(u.config).exists()}


# =====================================================================================
# the calendar: a weekday is a parameter, not a constant
# =====================================================================================
def test_a_thursday_series_does_not_read_its_own_expiries_as_holidays():
    """SENSEX expires on Thursday. Derived against Tuesday — the value hardcoded before
    this change — its three nearest expiries alone invent NINE holidays, including 25 Aug
    2026, which is a normal trading day and NIFTY's own expiry. That calendar refuses
    startup on real sessions and mis-computes dte on the rest."""
    thursdays = [dt.date(2026, 8, 20), dt.date(2026, 8, 27), dt.date(2026, 9, 3)]
    good = CALN.Calendar.build(expiries=thursdays, weekday=CALN.THURSDAY)
    assert good.holidays == frozenset()
    assert good.expiry_is_valid(thursdays[0])[0] is True

    wrong = CALN.Calendar.build(expiries=thursdays, weekday=CALN.TUESDAY)
    assert len(wrong.holidays) >= 8
    assert dt.date(2026, 8, 25) in wrong.holidays          # a real trading day


def test_the_calendar_remembers_its_own_weekday():
    """expiry_is_valid used to default to Tuesday whatever calendar it was called on, so a
    caller that built a Thursday calendar correctly could still validate against Tuesday."""
    cal = CALN.Calendar.build(expiries=[dt.date(2026, 8, 20)], weekday=CALN.THURSDAY)
    assert cal.weekday == CALN.THURSDAY
    assert cal.expiry_is_valid(dt.date(2026, 8, 20))[0] is True


def test_a_genuine_holiday_shift_is_still_accepted():
    """The point of deriving rather than asserting: a Thursday holiday moves expiry to
    Wednesday, and refusing that would be the check misfiring."""
    cal = CALN.Calendar.build(expiries=[dt.date(2026, 8, 19)],
                              extra=[dt.date(2026, 8, 20)], weekday=CALN.THURSDAY)
    ok, why = cal.expiry_is_valid(dt.date(2026, 8, 19))
    assert ok and "holiday" in why


def test_weekday_names_from_config_resolve():
    assert CALN.weekday_num("THURSDAY") == 3
    assert CALN.weekday_num("tuesday") == 1
    with pytest.raises(ValueError):
        CALN.weekday_num("EXPIRYDAY")


# =====================================================================================
# the far-dated gate: a monthly series is not a weekly one
# =====================================================================================
def _cfg(max_dte=None, allow_expiry=False):
    return {"session": {"stop_to_target_ratio": 0.5, "allow_expiry_day": allow_expiry,
                        "max_dte": max_dte, "reject_negative_dte": True,
                        "by_days_to_expiry": {
                            "3+": {"target_points": 5, "size_mult": 0.5},
                            "2": {"target_points": 7, "size_mult": 0.75},
                            "1": {"target_points": 10, "size_mult": 1.0},
                            "0": {"target_points": 10, "size_mult": 0.6}}}}


def test_a_far_dated_monthly_session_is_not_traded_on_a_weekly_table():
    """BANKNIFTY has no weeklies, so most of its month sits at dte 5..25 — every one of
    which lands in the '3+' row and is handed a target calibrated for an option about to
    expire. Intraday decay on a 22-day option is a fraction of a 3-day one, so the same
    target asks a much slower trade to travel the same distance against the same stop."""
    far = C.session_params(dt.date(2026, 8, 3), dt.date(2026, 8, 25), _cfg(max_dte=4))
    assert far.dte > 4 and far.tradeable is False
    assert "max_dte" in far.reason


def test_the_final_week_of_a_monthly_series_still_trades():
    near = C.session_params(dt.date(2026, 8, 24), dt.date(2026, 8, 25), _cfg(max_dte=4))
    assert near.dte == 1 and near.tradeable is True


def test_no_max_dte_configured_changes_nothing():
    """Absent, the gate must not exist. A default of 4 applied to a config that never asked
    for one would silently stop a weekly instrument from trading its furthest session."""
    p = C.session_params(dt.date(2026, 8, 3), dt.date(2026, 8, 25), _cfg(max_dte=None))
    assert p.dte > 4 and p.tradeable is True


def test_expiry_day_outranks_the_far_dated_gate():
    p = C.session_params(dt.date(2026, 8, 25), dt.date(2026, 8, 25), _cfg(max_dte=4))
    assert p.dte == 0 and p.tradeable is False
    assert "expiry day" in p.reason


def test_a_monthly_cycle_offers_far_fewer_sessions_than_a_month_of_weekdays():
    """The intuitive 20-21 sessions a month is wrong by roughly 5x once max_dte applies,
    and every expectancy estimate built on it would be too."""
    assert C.tradeable_sessions_per_cycle(_cfg(max_dte=4)) == 4
    assert C.tradeable_sessions_per_cycle(_cfg(max_dte=4, allow_expiry=True)) == 5


# =====================================================================================
# state is per instrument
# =====================================================================================
def test_every_instrument_keeps_its_own_journal_lockout_record_and_lock():
    """Sharing any one of these silently corrupts all three: a lockout is keyed by date
    alone, so SENSEX hitting its stop would halt NIFTY."""
    for attr in ("journal", "lockout", "forward", "lock"):
        paths = [getattr(u, attr)() for u in INS.REGISTRY.values()]
        assert len(set(paths)) == len(paths), f"{attr} collides across instruments"


def test_each_config_agrees_with_its_registry_entry():
    """The runner refuses to start on a mismatch, because a config pointing at another
    exchange would resolve a chain for an instrument the registry thinks is different."""
    for slug, cfg in CONFIGS.items():
        u = INS.get(slug)
        assert cfg["instrument"]["index_key"] == u.index_key
        assert cfg["instrument"]["exchange"] == u.exchange


def test_an_unknown_slug_is_refused_rather_than_defaulted():
    with pytest.raises(KeyError):
        INS.get("niftybank")


# =====================================================================================
# one account
# =====================================================================================
def test_the_allocations_do_not_oversubscribe_the_account():
    """This is the arithmetic the ledger exists to enforce, checked against the files
    themselves so a fourth instrument cannot be added without noticing."""
    total = sum(c["margin"]["max_utilisation_pct"] for c in CONFIGS.values())
    ceilings = {c["margin"]["portfolio_max_utilisation_pct"] for c in CONFIGS.values()}
    assert len(ceilings) == 1, "the account ceiling must be the same number everywhere"
    assert total <= ceilings.pop() + 1e-9


def test_a_dead_session_does_not_hold_capital_hostage(tmp_path):
    """Same reasoning as the session lock: a crash at 09:31 must not lock the account out
    for the rest of the day."""
    path = str(tmp_path / "c.json")
    ALLOC.commit("nifty", 900_000, path)
    assert ALLOC.committed_elsewhere("sensex", path) == 900_000

    import json
    data = json.loads(open(path).read())
    data["nifty"]["pid"] = 999_999_999                   # a pid that cannot be alive
    open(path, "w").write(json.dumps(data))
    assert ALLOC.committed_elsewhere("sensex", path) == 0


def test_a_commitment_from_another_day_is_ignored(tmp_path):
    path = str(tmp_path / "c.json")
    ALLOC.commit("nifty", 900_000, path, today=dt.date(2020, 1, 1))
    assert ALLOC.committed_elsewhere("sensex", path) == 0


def test_an_instrument_never_counts_its_own_commitment(tmp_path):
    path = str(tmp_path / "c.json")
    ALLOC.commit("nifty", 900_000, path)
    assert ALLOC.committed_elsewhere("nifty", path) == 0


def test_releasing_frees_the_capital(tmp_path):
    path = str(tmp_path / "c.json")
    ALLOC.commit("nifty", 900_000, path)
    ALLOC.release("nifty", path)
    assert ALLOC.committed_elsewhere("sensex", path) == 0


def test_a_corrupt_ledger_reads_as_empty_rather_than_raising(tmp_path):
    """It gates entry. A half-written file must not stop every instrument from trading."""
    path = tmp_path / "c.json"
    path.write_text("{not json")
    assert ALLOC.committed_elsewhere("nifty", str(path)) == 0


# =====================================================================================
# the entry window
# =====================================================================================
def _spec(tmp_path, **timing):
    cfg = {"timing": {"entry_window_end": "09:45", "no_new_entry_after": "12:30",
                      "force_exit": "15:10", **timing}}
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(cfg))
    return {"slug": "nifty", "label": "NIFTY", "config": str(p),
            "forward": str(tmp_path / "fwd.jsonl"),
            "journal": str(tmp_path / "j.jsonl"), "lock": str(tmp_path / "s.lock")}


def _ops(items):
    return [i["op"] for i in items]


def test_a_session_still_starts_after_the_opening_window(tmp_path):
    """The trigger is a human login at an unpredictable hour. Refusing to start at 10:30
    means the day is simply not traded, which is not what the window is for."""
    spec = _spec(tmp_path, allow_late_entry=True)
    items = AR._options_items(spec, now=dt.datetime(2026, 8, 19, 10, 30),
                              today=dt.date(2026, 8, 19),
                              entry_window_end=dt.time(9, 45))
    sess = next(i for i in items if i["op"] == "strangle_session")
    assert "late first entry" in sess["why"]
    assert "size it down" in sess["note"]


def test_a_late_session_is_still_refused_once_too_little_day_remains(tmp_path):
    """no_new_entry_after is the config's own boundary for opening anything new, used here
    rather than a number invented for this path."""
    spec = _spec(tmp_path, allow_late_entry=True)
    items = AR._options_items(spec, now=dt.datetime(2026, 8, 19, 13, 0),
                              today=dt.date(2026, 8, 19),
                              entry_window_end=dt.time(9, 45))
    assert "strangle_session" not in _ops(items)


def test_without_late_entry_the_window_still_closes_at_09_45(tmp_path):
    spec = _spec(tmp_path, allow_late_entry=False)
    items = AR._options_items(spec, now=dt.datetime(2026, 8, 19, 10, 30),
                              today=dt.date(2026, 8, 19),
                              entry_window_end=dt.time(9, 45))
    assert "strangle_session" not in _ops(items)


def test_nothing_starts_before_the_options_market_opens(tmp_path):
    spec = _spec(tmp_path, allow_late_entry=True)
    items = AR._options_items(spec, now=dt.datetime(2026, 8, 19, 8, 50),
                              today=dt.date(2026, 8, 19),
                              entry_window_end=dt.time(9, 45))
    assert items == []


def test_every_live_config_permits_the_late_entry_the_operator_asked_for():
    for slug, cfg in CONFIGS.items():
        tm = cfg["timing"]
        assert tm.get("allow_late_entry") is True, slug
        assert 0 < float(tm["late_entry_size_mult"]) <= 1.0, slug


# =====================================================================================
# the hardcoded token
# =====================================================================================
def test_the_runner_no_longer_hardcodes_the_nifty_index_token():
    """256265 is NIFTY 50. Left in place it computed BANKNIFTY's and SENSEX's support and
    resistance from NIFTY's candles — silently, because the call still succeeds and still
    returns levels. Checked as a literal in the AST so a comment mentioning it is fine."""
    tree = ast.parse(pathlib.Path("scripts/strangle.py").read_text())
    literals = [n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and n.value == 256265]
    assert not literals, "the NIFTY index token is still hardcoded in the runner"


def test_the_registry_carries_a_distinct_index_token_per_instrument():
    tokens = [u.index_token for u in INS.REGISTRY.values()]
    assert len(set(tokens)) == len(tokens)


# =====================================================================================
# the scaled parameters
# =====================================================================================
def test_points_are_scaled_to_each_index_rather_than_copied():
    """A target in absolute points means a different trade on a 77,000 index than on a
    24,000 one. Copying NIFTY's 10 points to SENSEX would ask for a third of the move."""
    targets = {slug: cfg["session"]["by_days_to_expiry"]["1"]["target_points"]
               for slug, cfg in CONFIGS.items()}
    assert targets["sensex"] > targets["banknifty"] > targets["nifty"]


def test_the_structural_ratio_is_identical_on_every_instrument():
    """stop_to_target_ratio is [STRUCTURAL]. Scaling the index must not become an excuse
    to fit it per instrument."""
    ratios = {c["session"]["stop_to_target_ratio"] for c in CONFIGS.values()}
    assert ratios == {0.50}


def test_no_instrument_ships_calibrated_reference_bands():
    """They cannot be reconstructed from history and must be collected forward. A config
    that shipped bands would let a session trade on numbers nobody measured."""
    for slug, cfg in CONFIGS.items():
        assert not cfg["reference_band"], slug
        assert cfg["reference_band_required_in_paper"] is True, slug


def test_every_instrument_is_still_paper_only():
    for slug, cfg in CONFIGS.items():
        assert cfg["meta"]["mode"] == "paper", slug
        assert cfg["live"]["enabled"] is False, slug
        assert cfg["live"]["min_paper_sessions_before_live"] >= 60, slug


def test_the_wings_stay_inside_the_catastrophic_cap_at_the_configured_size():
    """wing_width * units <= max_catastrophic_loss_pct * capital, checked per file rather
    than trusted, because both terms were changed for the new instruments."""
    for slug, cfg in CONFIGS.items():
        units = cfg["sizing"]["lots"] * cfg["instrument"]["lot_size"]
        tail = cfg["structure"]["wing_width_points"] * units
        cap = cfg["sizing"]["max_catastrophic_loss_pct"] * cfg["capital"]["total"]
        assert tail <= cap, f"{slug}: {tail:,.0f} > {cap:,.0f}"


def test_configured_lots_fit_the_instrument_s_own_allocation():
    """Each file must be self-consistent: the size it asks for has to fit the slice it is
    allowed, or every session refuses at sizing and the instrument never trades."""
    for slug, cfg in CONFIGS.items():
        usable = min(cfg["capital"]["cash"] + cfg["capital"]["pledged_market_value"]
                     * (1 - cfg["capital"]["pledge_haircut_pct"]),
                     2 * cfg["capital"]["cash"])
        allowed = usable * cfg["margin"]["max_utilisation_pct"]
        need = cfg["sizing"]["lots"] * cfg["margin"]["margin_per_lot_estimate"]
        assert need <= allowed, f"{slug}: needs {need:,.0f}, allowed {allowed:,.0f}"
