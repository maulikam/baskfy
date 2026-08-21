"""Runtime settings: resolution order, validation, audit, and what must stay locked."""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

from app import config as C
from app import main as M
from app.analytics import db
from app.analytics import settings as S


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    path = str(tmp_path / "p.db")
    monkeypatch.setattr(C, "DB_PATH", path)
    with db.connect(path) as c:
        db.migrate(c)
        yield c


@pytest.fixture(autouse=True)
def restore_config():
    """Settings mutate the config module, so put it back between tests."""
    before = {s.key: getattr(C, s.key, None) for s in S.SPECS}
    yield
    for k, v in before.items():
        setattr(C, k, v)


# =====================================================================================
# resolution order
# =====================================================================================
def test_resolution_prefers_database_then_env_then_code(conn, monkeypatch):
    eff = S.effective(conn)
    assert eff["REGIME_CONFIRM_DAYS"]["source"] in ("environment", "code default")

    monkeypatch.setenv("REGIME_CONFIRM_DAYS", "4")
    assert S.effective(conn)["REGIME_CONFIRM_DAYS"]["value"] == 4
    assert S.effective(conn)["REGIME_CONFIRM_DAYS"]["source"] == "environment"

    S.save(conn, {"REGIME_CONFIRM_DAYS": "5"})
    got = S.effective(conn)["REGIME_CONFIRM_DAYS"]
    assert got["value"] == 5 and got["source"] == "database"


def test_saving_takes_effect_without_a_restart(conn):
    """Consumers read C.<NAME> at call time, so applying the override is enough."""
    S.save(conn, {"STOP_VOL_MULT": "2.6"})
    assert C.STOP_VOL_MULT == 2.6

    from app.scoring import stop_from_vol
    tight = stop_from_vol(1000.0, 0.20)
    S.save(conn, {"STOP_VOL_MULT": "4.0"})
    assert stop_from_vol(1000.0, 0.20) < tight     # a wider multiple stops further away


def test_reset_hands_the_setting_back_to_env(conn):
    S.save(conn, {"REGIME_BUFFER_BPS": "250"})
    assert S.effective(conn)["REGIME_BUFFER_BPS"]["source"] == "database"
    S.reset(conn, ["REGIME_BUFFER_BPS"])
    assert S.effective(conn)["REGIME_BUFFER_BPS"]["source"] != "database"


def test_apply_at_startup_restores_overrides(conn):
    S.save(conn, {"REGIME_BUFFER_BPS": "175"})
    setattr(C, "REGIME_BUFFER_BPS", 150)           # simulate a fresh import
    S.apply_to_config(conn)
    assert C.REGIME_BUFFER_BPS == 175


def test_an_override_for_an_unknown_key_is_ignored(conn):
    conn.execute("INSERT INTO settings(key, value, updated_at) VALUES('GONE','1','now')")
    S.apply_to_config(conn)                        # must not raise


# =====================================================================================
# typing and per-field validation
# =====================================================================================
def test_type_errors_name_the_field(conn):
    with pytest.raises(S.SettingsError, match="Confirmation days"):
        S.save(conn, {"REGIME_CONFIRM_DAYS": "banana"})


def test_range_is_enforced(conn):
    with pytest.raises(S.SettingsError, match="below the minimum"):
        S.save(conn, {"REGIME_CONFIRM_DAYS": "0"})
    with pytest.raises(S.SettingsError, match="above the maximum"):
        S.save(conn, {"REGIME_BUFFER_BPS": "5000"})


def test_choice_is_enforced(conn):
    with pytest.raises(S.SettingsError, match="not one of"):
        S.save(conn, {"REGIME_MODE": "yolo"})
    S.save(conn, {"REGIME_MODE": "propose"})
    assert C.REGIME_MODE == "propose"


def test_intpair_parses_and_orders(conn):
    S.save(conn, {"TARGET_POSITIONS": "10,14"})
    assert C.TARGET_POSITIONS == (10, 14)
    with pytest.raises(S.SettingsError, match="exceeds maximum"):
        S.save(conn, {"TARGET_POSITIONS": "20,10"})


def test_bool_accepts_form_and_text_shapes(conn):
    for raw, expected in (("true", True), ("on", True), ("1", True),
                          ("false", False), ("", False)):
        S.save(conn, {"FULLY_INVESTED": raw})
        assert C.FULLY_INVESTED is expected


# =====================================================================================
# cross-field validation — a value can be legal alone and impossible together
# =====================================================================================
def test_position_count_that_cannot_fit_the_sleeve_is_refused(conn):
    with pytest.raises(S.SettingsError, match="only 100% exists"):
        S.save(conn, {"TARGET_POSITIONS": "12,30", "MIN_POSITION_WEIGHT": "6.0"})


def test_too_few_positions_to_absorb_the_sleeve_is_refused(conn):
    with pytest.raises(S.SettingsError, match="at least"):
        S.save(conn, {"TARGET_POSITIONS": "2,3", "MAX_SINGLE_WEIGHT": "15.0"})


def test_min_above_max_weight_is_refused(conn):
    with pytest.raises(S.SettingsError, match="exceeds max single weight"):
        S.save(conn, {"MIN_POSITION_WEIGHT": "20.0", "MAX_SINGLE_WEIGHT": "15.0"})


def test_stop_floor_above_ceiling_is_refused(conn):
    with pytest.raises(S.SettingsError, match="exceeds stop ceiling"):
        S.save(conn, {"STOP_MIN": "0.30", "STOP_MAX": "0.12"})


def test_r4_above_r3_is_refused(conn):
    with pytest.raises(S.SettingsError, match="at or below the R3 cap"):
        S.save(conn, {"REGIME_R4_EQUITY_PCT": "60"})


def test_r4_equity_without_a_residual_policy_is_refused(conn):
    """Otherwise R4 permits equity but names nobody to hold — the spec's own trap."""
    with pytest.raises(S.SettingsError, match="names no residual positions"):
        S.save(conn, {"REGIME_R4_EQUITY_PCT": "10", "REGIME_R4_MAX_RESIDUAL_NAMES": "0"})
    S.save(conn, {"REGIME_R4_EQUITY_PCT": "0", "REGIME_R4_MAX_RESIDUAL_NAMES": "0"})


def test_a_rejected_save_writes_nothing(conn):
    """All-or-nothing: a valid field must not slip through beside an invalid one."""
    before = C.REGIME_BUFFER_BPS
    with pytest.raises(S.SettingsError):
        S.save(conn, {"REGIME_BUFFER_BPS": "175", "REGIME_CONFIRM_DAYS": "0"})
    assert C.REGIME_BUFFER_BPS == before
    assert S.stored(conn) == {}


# =====================================================================================
# what must never be editable
# =====================================================================================
@pytest.mark.parametrize("key", sorted(S.SECRET_KEYS | S.LOCKED_KEYS))
def test_secrets_and_safety_switches_are_refused(conn, key):
    with pytest.raises(S.SettingsError, match="not editable"):
        S.save(conn, {key: "true"})


def test_credentials_are_never_in_the_spec():
    keys = {s.key for s in S.SPECS}
    assert not (keys & S.SECRET_KEYS)
    assert not (keys & S.LOCKED_KEYS)


def test_scoring_weights_are_not_exposed():
    keys = {s.key for s in S.SPECS}
    assert "MOMENTUM_BLEND" not in keys and "SHARPE_BLEND" not in keys


def test_unknown_keys_are_refused(conn):
    with pytest.raises(S.SettingsError, match="not a known setting"):
        S.save(conn, {"MADE_UP": "1"})


def test_locked_view_explains_each_one():
    for row in S.locked_view():
        assert row["why"], f"{row['key']} has no explanation"


# =====================================================================================
# audit
# =====================================================================================
def test_a_change_is_recorded_with_both_values(conn):
    S.save(conn, {"REGIME_BUFFER_BPS": "200"}, note="widen the band")
    h = S.history(conn)
    assert len(h) == 1
    assert h[0]["key"] == "REGIME_BUFFER_BPS"
    assert h[0]["new_value"] == "200" and h[0]["note"] == "widen the band"
    assert h[0]["old_value"] is not None


def test_a_no_op_save_is_not_recorded(conn):
    """Saving the form unchanged must not bury real changes under noise."""
    current = S.effective(conn)["REGIME_BUFFER_BPS"]["text"]
    S.save(conn, {"REGIME_BUFFER_BPS": current})
    assert S.history(conn) == []


def test_reset_is_itself_recorded(conn):
    S.save(conn, {"REGIME_BUFFER_BPS": "200"})
    S.reset(conn, ["REGIME_BUFFER_BPS"])
    assert any(h["new_value"] == "<reset>" for h in S.history(conn))


# =====================================================================================
# the page
# =====================================================================================
@pytest.fixture()
def client(monkeypatch, conn):
    monkeypatch.setattr(M, "_kite", None)
    return TestClient(M.app)


def test_settings_page_renders(client):
    r = client.get("/settings")
    assert r.status_code == 200
    for needle in ("Position sizing", "Risk limits", "Regime overlay",
                   "Not editable here", "Change history"):
        assert needle in r.text


def test_page_never_renders_a_credential(client):
    text = client.get("/settings").text
    assert "KITE_API_SECRET" not in text
    assert C.KITE_API_SECRET not in text
    assert C.KITE_API_KEY not in text


def test_form_post_applies_and_redirects(client, conn):
    r = client.post("/settings", data={"REGIME_BUFFER_BPS": "225",
                                       "_form_bools": "", "note": "test"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert C.REGIME_BUFFER_BPS == 225


def test_invalid_post_redirects_with_an_error_and_changes_nothing(client, conn):
    before = C.REGIME_CONFIRM_DAYS
    r = client.post("/settings", data={"REGIME_CONFIRM_DAYS": "0", "_form_bools": ""},
                    follow_redirects=False)
    assert r.status_code == 303 and "error=" in r.headers["location"]
    assert C.REGIME_CONFIRM_DAYS == before


def test_a_post_without_the_form_marker_leaves_booleans_alone(client, conn):
    """An unchecked box is absent from a form. Without the marker naming which boxes the
    form rendered, a partial API post would switch every boolean off."""
    S.save(conn, {"FULLY_INVESTED": "true"})
    assert C.FULLY_INVESTED is True
    client.post("/settings", data={"REGIME_BUFFER_BPS": "160"}, follow_redirects=False)
    assert C.FULLY_INVESTED is True, "a partial post silently cleared a boolean"


def test_the_form_marker_does_clear_an_unchecked_box(client, conn):
    S.save(conn, {"FULLY_INVESTED": "true"})
    client.post("/settings", data={"REGIME_BUFFER_BPS": "160",
                                   "_form_bools": "FULLY_INVESTED"},
                follow_redirects=False)
    assert C.FULLY_INVESTED is False


def test_json_endpoint_reports_values_and_sources(client, conn):
    S.save(conn, {"REGIME_BUFFER_BPS": "205"})
    d = client.get("/settings/data").json()
    assert d["settings"]["REGIME_BUFFER_BPS"]["value"] == "205"
    assert d["settings"]["REGIME_BUFFER_BPS"]["source"] == "database"
    assert any(r["key"] == "DRY_RUN" for r in d["locked"])


# =====================================================================================
# risk preview — a percentage is not a decision
# =====================================================================================
def test_inr_short_uses_lakh_and_crore():
    assert S.inr_short(10_531_889) == "1.05 cr"
    assert S.inr_short(526_594) == "5.27 L"
    assert S.inr_short(4_200) == "4,200"


def test_inr_short_handles_negatives_by_magnitude():
    assert S.inr_short(-12_000_000).endswith("cr")


def test_the_preview_multiplies_the_percentage_out():
    p = S.risk_preview(10_000_000.0)
    row = next(r for r in p["rows"] if r["key"] == "RISK_MAX_DAILY_LOSS_PCT")
    assert row["value"] == f"₹{10_000_000 * C.RISK_MAX_DAILY_LOSS_PCT / 100:,.0f}"


def test_the_preview_matches_what_the_gateway_will_enforce():
    """A preview that re-derived these would eventually disagree with the real limits."""
    nav = 10_531_889.0
    cfg = C.risk_config(nav)
    p = S.risk_preview(nav)
    shown = {r["key"]: r["value"] for r in p["rows"]}
    assert shown["RISK_MAX_DAILY_LOSS_PCT"] == f"₹{cfg.max_daily_loss:,.0f}"
    assert shown["RISK_POSITION_HEADROOM"] == f"₹{cfg.max_position_value:,.0f}"
    assert shown["RISK_GROSS_MULTIPLE"] == f"₹{cfg.max_gross_exposure:,.0f}"


def test_without_a_snapshot_the_preview_says_so_rather_than_inventing_a_nav():
    p = S.risk_preview(0.0)
    assert p["have_nav"] is False and p["problems"] == []


def test_an_incoherent_limit_is_surfaced_not_only_logged(monkeypatch):
    """A cap below the largest legal position blocks something the strategy permits."""
    monkeypatch.setattr(C, "RISK_MAX_POSITION_VALUE", 100.0)
    p = S.risk_preview(10_000_000.0)
    assert p["problems"] and "below the largest position" in p["problems"][0]


def test_the_page_explains_itself_when_there_is_no_snapshot(client):
    """Every risk limit is derived from NAV, so with no snapshot the page must say the
    gateway is on fallback defaults rather than print a number that means nothing."""
    page = client.get("/settings").text
    assert "No stored snapshot yet" in page
    assert "nothing to do with your book" in page


def test_the_page_shows_the_rupee_figures_once_a_snapshot_exists(client, conn):
    import json as _json
    conn.execute("INSERT INTO snapshots(date, nav, invested, cash, holdings_json,"
                 " index_value) VALUES('2026-08-14', 10531889.0, 9000000.0, 1531889.0,"
                 " ?, 100.0)", (_json.dumps([]),))
    conn.commit()
    page = client.get("/settings").text
    assert "the gateway enforces" in page
    assert "1.05 cr" in page
