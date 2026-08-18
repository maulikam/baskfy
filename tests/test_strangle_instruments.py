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


# =====================================================================================
# defects found reviewing the multi-instrument change itself
# =====================================================================================
def test_market_facts_uses_the_config_s_weekday_when_it_builds_its_own_calendar():
    """assert_market_facts built a TUESDAY calendar whenever no calendar was passed. For
    SENSEX that did not fail — which is worse. It PASSED, by inferring that the Tuesday
    after each Thursday expiry was a holiday, and reported an ordinary expiry as 'shifted
    back from 2026-08-25, a holiday'. Six real trading days were marked shut, so every dte
    derived from that calendar was short by however many fell inside the window."""
    from app.strategies.strangle import config as SC
    cfg = yaml.safe_load(open(INS.get("sensex").config))
    ins = [{"name": "SENSEX", "segment": "BFO-OPT", "lot_size": 20, "strike": k * 100.0,
            "expiry": exp, "instrument_type": t}
           for exp in (dt.date(2026, 8, 20), dt.date(2026, 8, 27))
           for k in range(770, 790) for t in ("CE", "PE")]

    facts = SC.assert_market_facts(cfg, ins, today=dt.date(2026, 8, 18))
    assert facts["expiry"] == dt.date(2026, 8, 20)
    assert facts["expiry_note"] == "on the expected weekday"
    assert facts["calendar"]["holidays"] == 0, "phantom holidays were invented"
    assert dt.date(2026, 8, 25) not in facts["calendar"]["known_future_holidays"]


def test_sessions_per_cycle_still_counts_four_when_no_max_dte_is_set():
    """Introducing max_dte quietly changed the default from 4 to 3, cutting every
    expectancy estimate by a quarter for any config predating it."""
    assert C.tradeable_sessions_per_week({"session": {"allow_expiry_day": False}}) == 4
    assert C.tradeable_sessions_per_week({"session": {"allow_expiry_day": True}}) == 5


# --- the entry window rule has exactly one definition ---------------------------------
def _win_cfg(late=True, mult=0.75):
    return {"timing": {"entry_window_end": "09:45", "no_new_entry_after": "12:30",
                       "force_exit": "15:10", "allow_late_entry": late,
                       "late_entry_size_mult": mult}}


def test_the_entry_window_states_are_open_late_then_closed():
    assert C.entry_window_state(_win_cfg(), dt.time(9, 30))["state"] == "open"
    late = C.entry_window_state(_win_cfg(), dt.time(10, 30))
    assert late["state"] == "late" and late["veto"] is None
    assert late["size_mult"] == 0.75
    assert C.entry_window_state(_win_cfg(), dt.time(13, 0))["state"] == "closed"


def test_late_entry_disabled_closes_the_window_at_the_configured_time():
    shut = C.entry_window_state(_win_cfg(late=False), dt.time(10, 30))
    assert shut["state"] == "closed" and "late entry is disabled" in shut["veto"]


def test_an_open_window_never_reduces_size():
    assert C.entry_window_state(_win_cfg(), dt.time(9, 15))["size_mult"] == 1.0


def test_a_missing_config_falls_back_to_a_closed_window_not_an_open_one():
    """autorun asks about instruments whose config may be unreadable. Defaulting to open
    would start a session against rules nobody could load."""
    st = C.entry_window_state(None, dt.time(10, 30))
    assert st["state"] == "closed" and st["size_mult"] == 0.0


def test_the_entry_window_rule_is_defined_exactly_once():
    """Three callers need this answer — the runner entering, --check reporting, and
    autorun deciding whether to start a process at all — and each grew its own copy.
    Both drift directions are silent: autorun starts a session that immediately refuses,
    or declines one that would have traded and the day is lost with no record of why."""
    defs = []
    for path in ("app/strategies/strangle/clock.py", "scripts/strangle.py",
                 "app/analytics/autorun.py"):
        tree = ast.parse(pathlib.Path(path).read_text())
        defs += [(path, n.name) for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef)
                 and n.name in ("entry_window_state", "_entry_window_state",
                                "_entry_deadline")]
    assert defs == [("app/strategies/strangle/clock.py", "entry_window_state")], defs


def test_no_caller_reparses_the_window_times_for_itself():
    """A second function splitting "09:45" on a colon is how the copies came back last
    time. force_exit is the one legitimate other time parse in the runner."""
    for path, allowed in (("scripts/strangle.py", 1), ("app/analytics/autorun.py", 0)):
        tree = ast.parse(pathlib.Path(path).read_text())
        splits = [n for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                  and n.func.attr == "split"
                  and any(isinstance(a, ast.Constant) and a.value == ":" for a in n.args)]
        assert len(splits) == allowed, f"{path} parses {len(splits)} time strings itself"


def test_a_margin_claim_is_released_however_the_process_exits(tmp_path):
    """The claim was made after sizing and released only on the two paths that reach the
    management loop; NO_DEPTH and ALREADY_RUNNING returned holding it. It is now released
    on interpreter exit as well — checked by running a real process rather than by finding
    an atexit.register call, because the call existing proves nothing about which FILE it
    releases.
    """
    import subprocess
    import sys
    ledger = str(tmp_path / "c.json")
    prog = ("import sys; sys.path.insert(0, %r);"
            "from app.strategies.strangle import allocation as A;"
            "A.commit('nifty', 850000, %r);" % (str(pathlib.Path.cwd()), ledger))
    for tail in ("", "raise SystemExit(3)"):
        subprocess.run([sys.executable, "-c", prog + tail], capture_output=True)
        assert ALLOC._read(ledger) == {}, f"the claim outlived the process ({tail or 'clean'})"


def test_the_release_targets_the_same_file_the_claim_was_written_to(tmp_path):
    """The runner used to register `atexit.register(release, slug)` with no path, which is
    correct only while every caller uses the default. commit() now registers its own
    release against the path it actually wrote to."""
    import subprocess
    import sys
    ledger = str(tmp_path / "other.json")
    subprocess.run([sys.executable, "-c",
                    "import sys; sys.path.insert(0, %r);"
                    "from app.strategies.strangle import allocation as A;"
                    "A.commit('sensex', 600000, %r)" % (str(pathlib.Path.cwd()), ledger)],
                   capture_output=True)
    assert ALLOC._read(ledger) == {}
    # and the default ledger was not touched on that instrument's behalf
    assert "sensex" not in ALLOC._read(ALLOC.PATH)


def test_a_hard_kill_leaves_a_claim_that_the_pid_check_still_discounts(tmp_path):
    """atexit cannot run on SIGKILL, so the row survives. live() must still not count it,
    or one crash would lock the account out for the rest of the day."""
    import subprocess
    import sys
    import time
    ledger = str(tmp_path / "c.json")
    p = subprocess.Popen([sys.executable, "-c",
                          "import sys, time; sys.path.insert(0, %r);"
                          "from app.strategies.strangle import allocation as A;"
                          "A.commit('nifty', 850000, %r); time.sleep(30)"
                          % (str(pathlib.Path.cwd()), ledger)])
    for _ in range(60):
        if "nifty" in ALLOC._read(ledger):
            break
        time.sleep(0.1)
    p.kill()
    p.wait()
    assert "nifty" in ALLOC._read(ledger), "the row should survive a kill"
    assert ALLOC.live(ledger) == {}, "a dead process still holds capital"


# --- the ops badge --------------------------------------------------------------------
def test_last_run_selects_the_id_its_link_needs():
    """The badge links to /ops/job/<id>. id was never selected, so Jinja resolved it to
    Undefined, the {% if %} guarding the link never fired, and the output link silently did
    not exist on the options page."""
    from app.analytics import db, ops
    with db.connect() as conn:
        rows = ops.last_run(conn)
    if rows:
        assert "id" in next(iter(rows.values()))


def test_a_run_is_attributed_to_the_instrument_it_was_given():
    from app.analytics import ops
    argv = '["py", "-m", "scripts.strangle", "--instrument", "sensex", "--collect"]'
    assert ops.argv_instrument(argv) == "sensex"
    assert ops.argv_instrument('["py", "-m", "scripts.daily"]') is None
    assert ops.argv_instrument(None) is None
    assert ops.argv_instrument("{not json") is None
    assert ops.argv_instrument('["py", "--instrument"]') is None       # no value follows


# --- the page speaks for every instrument, not the first one --------------------------
def test_the_next_action_is_computed_across_every_instrument():
    """The banner and the highlighted button read p.strangle, which is NIFTY. It would
    have kept saying 'collect' once NIFTY was calibrated and the other two were not."""
    from app.analytics import options_view as V
    blocks = [
        {"available": True, "label": "NIFTY", "bands_ready": {"ready": True},
         "next_action": {"op": "strangle_session", "label": "Run a paper session",
                         "why": "0 of 60"}},
        {"available": True, "label": "BANKNIFTY", "bands_ready": {"ready": False},
         "next_action": {"op": "strangle_collect", "label": "Record today's straddle",
                         "why": "no bands"}},
        {"available": True, "label": "SENSEX", "bands_ready": {"ready": False},
         "next_action": {"op": "strangle_collect", "label": "Record today's straddle",
                         "why": "no bands"}},
    ]
    nx = V._next_overall(blocks)
    assert nx["op"] == "strangle_collect"            # what two of three are blocked on
    assert nx["instruments"] == ["BANKNIFTY", "SENSEX"]
    assert nx["ready"] is False


def test_the_page_survives_one_unreadable_config():
    """The whole section was gated on NIFTY being loadable, so a typo in one file hid all
    three instruments and every control with them."""
    from app.analytics import options_view as V
    nx = V._next_overall([
        {"available": False, "label": "NIFTY", "error": "bad yaml"},
        {"available": True, "label": "SENSEX", "bands_ready": {"ready": False},
         "next_action": {"op": "strangle_collect", "label": "Record", "why": "no bands"}},
    ])
    assert nx["op"] == "strangle_collect" and nx["instruments"] == ["SENSEX"]


def test_no_instrument_configured_is_reported_rather_than_crashing():
    from app.analytics import options_view as V
    nx = V._next_overall([])
    assert nx["op"] is None and nx["instruments"] == []


def test_concurrent_commits_neither_clobber_nor_crash(tmp_path):
    """Three sessions committing at once. Before the lock this lost entries; before the
    unique temp name it RAISED, because each process replaced the same ".tmp" out from
    under the others and the loser died mid-commit with FileNotFoundError. autorun starts
    the instruments back to back, which is the case most likely to hit both."""
    import subprocess
    import sys
    import textwrap
    script = tmp_path / "hammer.py"
    script.write_text(textwrap.dedent(f'''
        import sys
        sys.path.insert(0, {str(pathlib.Path.cwd())!r})
        from app.strategies.strangle import allocation as A
        for _ in range(40):
            # release_on_exit=False: this test is about the read-modify-write, and
            # a child that tidied up after itself would leave nothing to measure.
            A.commit(sys.argv[2], float(sys.argv[3]), sys.argv[1],
                     release_on_exit=False)
    '''))
    ledger = str(tmp_path / "c.json")
    kids = [subprocess.Popen([sys.executable, str(script), ledger, slug, str(m)],
                             stderr=subprocess.PIPE)
            for slug, m in (("nifty", 9e5), ("banknifty", 5e5), ("sensex", 6e5))]
    errs = [(k.communicate()[1] or b"").decode() for k in kids]
    assert all(k.returncode == 0 for k in kids), f"a commit crashed: {errs}"
    assert sorted(ALLOC._read(ledger)) == ["banknifty", "nifty", "sensex"]


def test_no_temp_file_is_left_behind(tmp_path):
    ledger = tmp_path / "c.json"
    ALLOC.commit("nifty", 900_000, str(ledger))
    assert not list(tmp_path.glob("*.tmp"))


def test_check_reports_a_real_size_even_when_the_window_is_shut():
    """A closed window carries size_mult 0.0 — correct for "may I enter", nonsense for
    "how big would this be". Multiplying by it made --check report that the multipliers
    had asked for 0 lots."""
    from app.strategies.strangle import sizing as Z
    cfg = yaml.safe_load(open(INS.get("sensex").config))
    shut = C.entry_window_state(cfg, dt.time(23, 0))
    assert shut["veto"] and shut["size_mult"] == 0.0
    mult = 1.0 if shut["veto"] else shut["size_mult"]
    detail = Z.session_lots_detail(cfg, 1.0 * mult)
    assert detail["requested"] >= 1, "check would report a zero-lot request"


def test_the_min_lots_floor_is_reported_when_it_overrides():
    """On the "3+" bucket every instrument already sits at min_lots, so the late-entry
    haircut changes nothing there. Left unreported, the config reads as though 37.5% of
    base size were being taken when the position is actually the floor."""
    from app.strategies.strangle import sizing as Z
    for slug in INS.all_slugs():
        cfg = yaml.safe_load(open(INS.get(slug).config))
        far = float(cfg["session"]["by_days_to_expiry"]["3+"]["size_mult"])
        d = Z.session_lots_detail(cfg, far * 0.75)
        assert d["floored"] is True and d["note"], slug
        assert d["lots"] == cfg["sizing"]["min_lots"], slug
    # and it does NOT claim a floor when the multipliers genuinely win
    cfg = yaml.safe_load(open(INS.get("nifty").config))
    near = Z.session_lots_detail(cfg, 1.0)
    assert near["floored"] is False and near["lots"] == cfg["sizing"]["lots"]


def test_far_dated_observations_would_veto_every_tradeable_session():
    """Pins the DIRECTION of the contamination, which is the dangerous part. Pooling a
    monthly series' far-dated straddles lifts the band about twofold, so its lower gate
    sits above any genuine final-week straddle and every real session is refused as "IV
    below band" — a calibration bug wearing the costume of a market condition."""
    from app.strategies.strangle import calibrate as CAL
    obs = [CAL.Observation(session=dt.date(2026, 8, 20), expiry=dt.date(2026, 8, 25),
                           dte=d, bucket=CAL.bucket_for(d), spot=57000, strike=57000,
                           call=asp / 2, put=asp / 2)
           for d, asp in [(1, 700), (2, 760), (3, 790), (4, 810)]]
    obs += [CAL.Observation(session=dt.date(2026, 8, 3), expiry=dt.date(2026, 8, 25),
                            dte=d, bucket=CAL.bucket_for(d), spot=57000, strike=57000,
                            call=(700 + d * 80) / 2, put=(700 + d * 80) / 2)
            for d in range(5, 19)]

    dirty = CAL.build_bands(obs, min_samples=3)["3+"]
    clean = CAL.build_bands(obs, min_samples=3, max_dte=4)["3+"]

    real_straddle = 800.0                       # a genuine final-week observation
    assert dirty["low"] * 0.85 > real_straddle, "the contaminated band must veto it"
    assert clean["low"] * 0.85 <= real_straddle <= clean["high"] * 1.30
    assert CAL.excluded_beyond(obs, 4) == 14


def test_the_bands_mapping_never_carries_a_non_bucket_key():
    """It is handed straight to the rules engine as reference_band and iterated by the
    page, so a bookkeeping key inside it becomes a phantom bucket in both."""
    from app.strategies.strangle import calibrate as CAL
    obs = [CAL.Observation(session=dt.date(2026, 8, 20), expiry=dt.date(2026, 8, 25),
                           dte=d, bucket=CAL.bucket_for(d), spot=57000, strike=57000,
                           call=400, put=400) for d in (1, 2, 3, 9, 15)]
    bands = CAL.build_bands(obs, min_samples=1, max_dte=4)
    assert all(k in ("3+", "2", "1", "0") for k in bands), sorted(bands)


def test_readiness_states_how_many_observations_it_discarded():
    from app.strategies.strangle import calibrate as CAL
    r = CAL.readiness({"3+": {"sufficient": True}, "2": {"sufficient": True},
                       "1": {"sufficient": True}}, excluded=14)
    assert r["excluded_beyond_max_dte"] == 14
    assert "beyond max_dte" in r["note"]


# =====================================================================================
# no defaults that mean NIFTY
# =====================================================================================
def test_reconstruction_refuses_to_guess_an_index_token():
    """collect() fell back to 256265 — NIFTY 50 — whenever it could not find the index in
    the rows it was given. A SENSEX run whose BSE dump had been forgotten would compute ATM
    strikes near 24,000 for a chain that lives near 77,000: every lookup misses, the run
    reports NO_DATA, and nothing anywhere says the wrong index was used. The runtime
    fixtures had themselves been relying on it."""
    from app.strategies.strangle import calibrate as CAL
    opts = [{"name": "SENSEX", "segment": "BFO-OPT", "expiry": dt.date(2026, 8, 20),
             "strike": 77000.0, "instrument_type": "CE", "lot_size": 20,
             "instrument_token": 1, "tradingsymbol": "X", "exchange": "BFO"}]
    with pytest.raises(RuntimeError, match="refusing to guess a token"):
        CAL.collect(object(), instruments=opts, index_key="BSE:SENSEX", name="SENSEX",
                    lookback_days=30, step=100, today=dt.date(2026, 8, 18))


@pytest.mark.parametrize("fn,kwargs", [
    ("build_from_kite", {"index_token": 1, "exchange": "NFO"}),      # no name
    ("build_from_kite", {"name": "NIFTY", "exchange": "NFO"}),       # no index_token
    ("build_from_kite", {"name": "NIFTY", "index_token": 1}),        # no exchange
])
def test_the_calendar_will_not_assume_an_underlying(fn, kwargs):
    """These defaulted to NIFTY, NIFTY's token and NFO, so one forgotten argument derived
    NIFTY's holidays and expiry weekday for whatever was actually being asked about."""
    with pytest.raises(TypeError):
        getattr(CALN, fn)(object(), **kwargs)


def test_the_chain_snapshot_will_not_assume_an_underlying():
    from app.strategies.strangle import market as MK
    with pytest.raises(TypeError):
        MK.snapshot(object(), instruments=[], index_key="BSE:SENSEX",
                    expiry=dt.date(2026, 8, 20))


def test_the_calendar_reuses_a_dump_the_caller_already_holds():
    """Every runner invocation fetched its exchange dump twice — once for the chain, once
    inside the calendar. At three underlyings that was six fetches of up to 35,000 rows
    before a session could start."""
    class KC:
        fetches = 0
        def historical_data(self, *a, **k):
            return [{"date": dt.datetime(2026, 8, 17), "close": 1.0}]
        def instruments(self, exchange):
            KC.fetches += 1
            return []
    kc = KC()
    dump = [{"name": "SENSEX", "expiry": dt.date(2026, 8, 20)}]
    CALN.build_from_kite(kc, name="SENSEX", index_token=265, exchange="BFO",
                         weekday=CALN.THURSDAY, instruments=dump,
                         today=dt.date(2026, 8, 18))
    assert KC.fetches == 0, "the calendar re-fetched a dump it was handed"

    CALN.build_from_kite(kc, name="SENSEX", index_token=265, exchange="BFO",
                         weekday=CALN.THURSDAY, today=dt.date(2026, 8, 18))
    assert KC.fetches == 1, "the calendar must still fetch when given nothing"


def test_the_page_computes_each_instrument_block_once():
    """page() returned both `strangle` (NIFTY) and `strangles`, computing the NIFTY block
    twice — each one reads a config, a journal and a forward record."""
    from app.analytics import db, options_view as V
    calls = {"n": 0}
    real = V.strangle

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    V.strangle = counting
    try:
        with db.connect() as conn:
            pg = V.page(conn)
    finally:
        V.strangle = real
    assert calls["n"] == len(INS.configured())
    assert pg["strangle"]["slug"] == "nifty"


# =====================================================================================
# whether the configured size is economic at all
# =====================================================================================
def test_flat_brokerage_drag_is_reported_per_bucket():
    """Options brokerage is a FLAT Rs 20 per order with no percentage cap, so four legs
    cost Rs 80 however small the position. Splitting one account three ways cut BANKNIFTY
    to 2 lots in the far-dated bucket, where 60 units carry 1.33 points of brokerage
    against a 3.00-point entry-cost budget. The gate vetoing that is the gate telling the
    truth; what was missing is any way to see it before watching a run of silent vetoes."""
    from app.strategies.strangle import sizing as Z
    cfg = yaml.safe_load(open(INS.get("banknifty").config))
    far = Z.fixed_cost_drag(cfg, "3+")
    assert far["units"] == 60
    assert far["brokerage_points"] == pytest.approx(80 / 60, abs=0.01)
    assert far["drag_pct"] > 40
    assert far["note"], "an uneconomic bucket must say so"

    near = Z.fixed_cost_drag(cfg, "1")
    assert near["drag_pct"] < 15 and not near["note"]


def test_the_full_size_bucket_is_economic_on_every_instrument():
    """dte=1 is the one session that runs full target at full size. If flat costs ate the
    budget there too, the instrument could not be traded at all at this allocation."""
    from app.strategies.strangle import sizing as Z
    for slug in INS.all_slugs():
        cfg = yaml.safe_load(open(INS.get(slug).config))
        d = Z.fixed_cost_drag(cfg, "1")
        assert d["drag_pct"] < 20, f"{slug}: {d['drag_pct']}% of the budget is brokerage"


def test_a_naked_structure_is_charged_for_two_legs_not_four():
    from app.strategies.strangle import sizing as Z
    cfg = yaml.safe_load(open(INS.get("nifty").config))
    hedged = Z.fixed_cost_drag(cfg, "1")
    naked = Z.fixed_cost_drag({**cfg, "structure": {**cfg["structure"], "type": "NAKED"}},
                              "1")
    # abs tolerance: the field is rounded to three places, so 0.137/2 reads as 0.068
    assert naked["brokerage_points"] == pytest.approx(hedged["brokerage_points"] / 2,
                                                      abs=0.001)


# =====================================================================================
# the window has a floor as well as a ceiling
# =====================================================================================
def test_the_entry_window_is_shut_before_the_market_opens():
    """entry_window_state only ever checked `now <= end`, so every hour before the open
    reported "open" — 01:38 and 06:00 alike — and that is the one field answering "may I
    enter". Caught by running --check in the middle of the night."""
    for t in (dt.time(1, 38), dt.time(6, 0), dt.time(9, 14)):
        st = C.entry_window_state(_win_cfg(), t)
        assert st["state"] == "premarket", f"{t} reported {st['state']}"
        assert st["veto"] and st["size_mult"] == 0.0


def test_the_floor_is_the_configured_first_tick_not_a_constant():
    cfg = _win_cfg()
    cfg["timing"]["entry_early"] = "09:20"
    assert C.entry_window_state(cfg, dt.time(9, 17))["state"] == "premarket"
    assert C.entry_window_state(cfg, dt.time(9, 21))["state"] == "open"


def test_the_window_opens_exactly_at_the_first_tick():
    cfg = _win_cfg()
    assert C.entry_window_state(cfg, dt.time(9, 15))["state"] == "open"
    assert C.entry_window_state(cfg, dt.time(9, 14, 59))["state"] == "premarket"


def test_every_live_config_declares_its_first_tradeable_tick():
    for slug, cfg in CONFIGS.items():
        assert cfg["timing"].get("entry_early"), slug


def test_autorun_will_not_start_a_premarket_session(tmp_path):
    """It refused already, from its OWN copy of the constant — which is how the floor came
    to be enforced in one module and absent from the other."""
    spec = _spec(tmp_path, allow_late_entry=True, entry_early="09:15")
    items = AR._options_items(spec, now=dt.datetime(2026, 8, 19, 6, 0),
                              today=dt.date(2026, 8, 19),
                              entry_window_end=dt.time(9, 45))
    assert "strangle_session" not in [i["op"] for i in items]


def test_the_market_open_time_is_defined_once():
    """autorun held its own OPTIONS_OPEN while clock had no floor at all, so the same fact
    was enforced in one place and missing from the other."""
    import app.analytics.autorun as AR_mod
    from app.strategies.strangle import clock as CK
    assert AR_mod.OPTIONS_OPEN is CK.OPTIONS_OPEN
    src = pathlib.Path("app/analytics/autorun.py").read_text()
    tree = ast.parse(src)
    assigned = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                and any(getattr(t, "id", "") == "OPTIONS_OPEN" for t in n.targets)]
    assert not assigned, "autorun redeclared the market open time"


def test_a_premarket_run_does_not_consume_the_day(tmp_path):
    """autorun reads the journal to decide whether the day has been dealt with, and counts
    entry_window_closed as "the session had its say". A run before the open — the /options
    button at 08:00, or a cron a few minutes early — wrote exactly that, so autorun skipped
    the real session at 09:30 and the day was lost with no record of why.

    A run that declined to start has evaluated nothing and must not stand in for the
    session it was too early to be."""
    import json
    j = tmp_path / "j.jsonl"
    j.write_text(json.dumps({"ts": "2026-08-19T08:00:00",
                             "event": "entry_window_not_open",
                             "reason": "before 09:15"}) + "\n")
    assert AR._session_ran_today(str(j), dt.date(2026, 8, 19)) is False

    spec = {"slug": "nifty", "label": "NIFTY", "config": None,
            "forward": str(tmp_path / "f.jsonl"), "journal": str(j),
            "lock": str(tmp_path / "s.lock")}
    items = AR._options_items(spec, now=dt.datetime(2026, 8, 19, 9, 30),
                              today=dt.date(2026, 8, 19),
                              entry_window_end=dt.time(9, 45))
    assert "strangle_session" in [i["op"] for i in items], \
        "the pre-open refusal still consumed the day"


def test_a_genuine_refusal_after_the_cutoff_does_consume_the_day(tmp_path):
    """The other direction. Past no_new_entry_after the runner HAS decided, and starting
    another process at 13:00 to be refused again is noise."""
    import json
    j = tmp_path / "j.jsonl"
    j.write_text(json.dumps({"ts": "2026-08-19T13:00:00",
                             "event": "entry_window_closed",
                             "reason": "past 12:30"}) + "\n")
    assert AR._session_ran_today(str(j), dt.date(2026, 8, 19)) is True


def test_every_decided_event_is_a_real_outcome():
    """The list is what stops the day being re-run. Anything on it must mean the strategy
    evaluated the day, not that the runner declined to start."""
    assert "entry_window_not_open" not in AR.DECIDED
    assert set(AR.DECIDED) == {"session_closed", "entry_window_closed", "skipped",
                               "no_entry", "entry_cost_veto"}


def test_the_runner_reports_premarket_as_not_open_rather_than_closed():
    """At 06:00 the old note read 'only 15:10 remains and a decay trade cannot reach its
    target' — the whole day remained. A status that contradicts itself is worse than none."""
    src = pathlib.Path("scripts/strangle.py").read_text()
    assert "ENTRY_WINDOW_NOT_OPEN" in src
    assert "entry_window_not_open" in src
