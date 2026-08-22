"""The user/system settings boundary — docs/03 §3f, MERGE-PROMPTS.md M4 step 3.

docs/03 §3f draws the line and says why it is not a preference:

    "Knobs that belong to a basket (weights, caps, buffers, cash bands) move into a versioned
     BasketDefinition... Knobs that belong to the system (rate limits, risk ceilings, kill
     switch) stay as server config and are never user-editable. This split is a security
     boundary, not a preference: RISK_MAX_DAILY_LOSS_PCT must not become a form field."

Today the desk has one user and that user is the operator, so nothing is escalated by letting
them raise their own ceiling. Under multi-tenancy (P4) the same form is privilege escalation,
and the boundary has to exist before the second account does, not after.

`app/analytics/settings.py` already enforces two thirds of this by itself, and reasons about
it in its own docstring: credentials are never stored or rendered, and the safety switches
(DRY_RUN, INTRADAY_ENABLED, OPTIONS_ENABLED) are env-only because "putting them one click away
from /execute in a browser removes the deliberate friction that makes them trustworthy".

This file asserts the third: risk ceilings, the kill switch and the order-rate cap.
"""
from __future__ import annotations

import pytest

from app.analytics import settings as st

#: Every knob that decides how much can be lost or how fast orders can leave.
#: RISK_MAX_DAILY_LOSS_PCT is the kill switch — the desk's own .env.example says so.
SYSTEM_ONLY = (
    "RISK_MAX_POSITION_VALUE",
    "RISK_MAX_GROSS_EXPOSURE",
    "RISK_POSITION_HEADROOM",
    "RISK_GROSS_MULTIPLE",
    "RISK_MAX_DAILY_LOSS_PCT",
    "RISK_MAX_ORDERS_PER_DAY",
)


def _editable(key: str) -> bool:
    """Could a browser change this key through the settings route?"""
    return key in st.BY_KEY and key not in st.SECRET_KEYS and key not in st.LOCKED_KEYS


class TestCredentialsAndSwitches:
    """The two thirds settings.py already enforces. Pinned so they cannot regress."""

    @pytest.mark.parametrize("key", ["KITE_API_KEY", "KITE_API_SECRET"])
    def test_credentials_are_never_editable(self, key: str) -> None:
        assert key in st.SECRET_KEYS
        assert not _editable(key)

    @pytest.mark.parametrize("key", ["DRY_RUN", "INTRADAY_ENABLED", "OPTIONS_ENABLED"])
    def test_safety_switches_are_env_only(self, key: str) -> None:
        assert key in st.LOCKED_KEYS
        assert not _editable(key)


class TestRiskCeilings:
    """docs/03 §3f's own example. A ceiling a user can raise is not a ceiling."""

    @pytest.mark.parametrize("key", SYSTEM_ONLY)
    def test_risk_ceiling_is_not_reachable_from_the_settings_route(self, key: str) -> None:
        assert not _editable(key), (
            f"{key} is editable from /settings. docs/03 §3f: system knobs are never "
            f"user-editable. Add it to settings.LOCKED_KEYS and drop its Spec."
        )

    def test_no_risk_key_at_all_leaks_in_by_prefix(self) -> None:
        """Catches a RISK_* knob added later without reading any of this."""
        leaked = sorted(k for k in st.BY_KEY if k.startswith("RISK_") and _editable(k))
        assert leaked == [], f"new user-editable risk knobs: {leaked}"


class TestTheSettingsPageItself:
    """`save()` refusing a key is the enforcement; this asserts the surface agrees.

    A form that renders a field it will then refuse is a worse interface than one that does
    not render it, and it invites someone to "fix" the refusal later.
    """

    def test_no_risk_key_renders_as_an_input(self) -> None:
        from fastapi.testclient import TestClient

        from app.main import app

        page = TestClient(app).get("/settings").text
        for key in SYSTEM_ONLY:
            assert f'name="{key}"' not in page, f"{key} is still a form field on /settings"
            assert f'id="{key}"' not in page, f"{key} still renders an input on /settings"

    def test_the_ceilings_are_still_shown_even_though_they_cannot_be_changed(self) -> None:
        """Being unable to change a limit is no reason to be unable to see it.

        The read-only preview is the compensating control for the settings_audit row that
        locking these removed, alongside the startup log in app.main._log_risk_ceilings.

        The NAV is seeded here rather than borrowed. Every RISK_* limit is a percentage of NAV,
        so the page renders the rupee table only when a snapshot exists — and this test used to
        pass by reading whatever NAV happened to be in the desk's real database. It was therefore
        also a test that the developer's own machine had traded, which is not a property of the
        code. `tests/conftest.py`'s database isolation exposed it.
        """
        from fastapi.testclient import TestClient

        from app.analytics import db
        from app.main import app

        with db.connect() as conn:
            db.migrate(conn)
            db.upsert_snapshot(
                conn,
                {
                    "date": "2026-08-19",
                    "nav": 5_000_000.0,
                    "invested": 4_900_000.0,
                    "cash": 100_000.0,
                    "holdings_json": [],
                },
            )

        page = TestClient(app).get("/settings").text
        assert "Risk limits" in page
        assert "Not editable from this page" in page
        assert "RISK_MAX_DAILY_LOSS_PCT" in page
