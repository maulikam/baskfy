"""TW3: the sleeve's bounds — and the one that runs the other way.

``docs/twt/02-scope-and-gating.md`` §4 gives five settings and four server-side bounds. Three are
ceilings. **One is a floor**, and getting it backwards is the single most plausible defect in
this module, so it has its own section below, its own problem type, its own ``_MIN`` suffix and
its own named tests.

> ``BASKFY_TWT_TRAIL_PCT_MIN`` [18.00] — a **floor**, because tightening this one is the
> dangerous direction (`01` §5: 15 % halves the CAGR and doubles the drawdown).

The trail is TWT-1's only exit — 137 of the research's 164 exits are the trailing stop — so the
measured cliff is in the tightening direction: 20 % → 15 % took the CAGR from 20.9 % to 9.6 % and
the drawdown from -24.7 % to -43 %. Widening it is merely unprofitable (30 % → 15.5 % on 73
trades); tightening it is the failure mode. DECISIONS-TW **TW0.5**.

Everything here is pure: the bounds are arithmetic over a frozen dataclass, and the refusals are
exceptions. The database half of the same claim — that a refused patch writes **nothing at all**
— is in ``test_twt_schema.py``, where there is a database to prove it against.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest

from baskfy_api.app import _DOCUMENTED_ERRORS
from baskfy_api.problems import Problem, ProblemType
from baskfy_api.settings import Settings
from baskfy_api.twt_settings import (
    CEILING_ENV,
    EDITABLE_FIELDS,
    FLOOR_ENV,
    STRATEGY_MAX_SLOTS,
    SYSTEM_OWNED_FIELDS,
    TwtCeilings,
    TwtConfigPatch,
    to_view,
)
from baskfy_core.models import TwConfig
from baskfy_worker.settings import WorkerSettings

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
MONOREPO_ROOT: Final = Path(__file__).resolve().parents[4]
SCOPE: Final = (MONOREPO_ROOT / "docs" / "twt" / "02-scope-and-gating.md").read_text(
    encoding="utf-8"
)
BUSINESS_RULES: Final = (MONOREPO_ROOT / "docs" / "twt" / "04-business-rules.md").read_text(
    encoding="utf-8"
)

#: Both committed environment examples. The monorepo root's is the one
#: ``services/api/tests`` scans; ``decile-blueprint``'s is the one a reader of this tree opens.
#: A bound that is only in one of them is a bound half the deployments never see.
ENV_EXAMPLES: Final[tuple[Path, ...]] = (
    MONOREPO_ROOT / ".env.example",
    REPO_ROOT / ".env.example",
)


def _ceilings() -> TwtCeilings:
    """The shipped defaults, read off the field declarations rather than off an instance.

    An instance reads ``.env``, so a machine with one of these exported would make the
    assertions pass or fail for a reason that has nothing to do with what this repository ships.
    """
    return TwtCeilings(
        max_open_positions=Settings.model_fields["twt_max_open_positions_max"].default,
        max_position_pct=Settings.model_fields["twt_max_position_pct_max"].default,
        stop_pct=Settings.model_fields["twt_stop_pct_max"].default,
        trail_pct_min=Settings.model_fields["twt_trail_pct_min"].default,
    )


class TestTheThreeCeilings:
    def test_a_value_at_the_ceiling_is_allowed(self) -> None:
        """The ceiling is a maximum, not an exclusive bound — a 25 % stop is legal."""
        _ceilings().check("stop_pct", Decimal("25.00"))

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("stop_pct", Decimal("25.01")),
            ("max_position_pct", Decimal("15.01")),
            ("max_open_positions", 16),
        ],
    )
    def test_a_value_above_the_ceiling_is_refused_with_the_ceiling_named(
        self, field: str, value: Decimal | int
    ) -> None:
        """A refusal that says only "too large" invites the caller to bisect their way to the
        limit, and tells them nothing about *who* set it."""
        with pytest.raises(Problem) as caught:
            _ceilings().check(field, value)
        problem = caught.value
        assert problem.status == 422
        assert problem.type is ProblemType.SETTING_ABOVE_CEILING
        assert problem.extra["field"] == field
        assert problem.extra["requested"] == str(value)
        assert problem.extra["env_var"] == CEILING_ENV[field]
        assert str(problem.extra["ceiling"]) in problem.detail

    @pytest.mark.parametrize("field", sorted(CEILING_ENV))
    def test_every_ceiling_field_has_an_env_var_ending_in_max(self, field: str) -> None:
        assert CEILING_ENV[field].startswith("BASKFY_TWT_")
        assert CEILING_ENV[field].endswith("_MAX")

    def test_sleeve_capital_has_no_bound_at_all(self) -> None:
        """It is the person's own money, not a risk multiplier. It is also seeded at 0 and
        never set by anything in this repository (`02` §3)."""
        _ceilings().check("sleeve_capital_inr", Decimal("100000000"))
        assert "sleeve_capital_inr" not in CEILING_ENV
        assert "sleeve_capital_inr" not in FLOOR_ENV


class TestTheOneBoundThatIsAFloor:
    """DECISIONS-TW **TW0.5**, and the easiest thing in TW3 to implement backwards.

    Every other bounded setting in this repository is capped above. This one is not, and a test
    that only checked "a bad value is refused" would pass just as happily on the mirror image.
    So the three cases are asserted separately: below refuses, at is allowed, and **above is
    allowed too** — because a widened trail is conservative rather than dangerous.
    """

    def test_a_trail_below_the_floor_is_refused_with_the_floor_named(self) -> None:
        with pytest.raises(Problem) as caught:
            _ceilings().check("trail_pct", Decimal("15.00"))
        problem = caught.value
        assert problem.status == 422
        assert problem.type is ProblemType.SETTING_BELOW_FLOOR
        assert problem.extra["field"] == "trail_pct"
        assert problem.extra["requested"] == "15.00"
        assert problem.extra["floor"] == "18.00"
        assert problem.extra["env_var"] == "BASKFY_TWT_TRAIL_PCT_MIN"
        assert "18.00" in problem.detail

    def test_a_trail_at_the_floor_is_allowed(self) -> None:
        """The floor is a minimum, not an exclusive bound."""
        _ceilings().check("trail_pct", Decimal("18.00"))

    @pytest.mark.parametrize("value", ["20.00", "25.00", "30.00", "40.00"])
    def test_a_wider_trail_is_allowed_because_widening_is_not_the_failure_mode(
        self, value: str
    ) -> None:
        """**The test that catches the backwards implementation.**

        If ``trail_pct`` had been given a ceiling instead of a floor, every one of these would
        raise — and the sleeve would refuse a setting that is merely conservative while
        cheerfully accepting the 15 % that halves the CAGR and doubles the drawdown.
        """
        _ceilings().check("trail_pct", Decimal(value))

    def test_the_floor_is_the_documents_number(self) -> None:
        assert _ceilings().trail_pct_min == Decimal("18.00")
        assert "BASKFY_TWT_TRAIL_PCT_MIN` [18.00]" in SCOPE
        assert "a **floor**" in SCOPE

    def test_the_floors_env_var_says_min_and_not_max(self) -> None:
        """The suffix is half the documentation. ``_MAX`` here would be a lie a reader believes."""
        assert FLOOR_ENV == {"trail_pct": "BASKFY_TWT_TRAIL_PCT_MIN"}
        assert not FLOOR_ENV["trail_pct"].endswith("_MAX")

    def test_a_field_is_never_both_a_ceiling_and_a_floor(self) -> None:
        assert set(CEILING_ENV) & set(FLOOR_ENV) == set()

    def test_adding_the_floors_type_did_not_rewrite_every_422_in_the_contract(self) -> None:
        """A regression this module actually caused, caught, and pinned.

        ``app._DOCUMENTED_ERRORS`` builds one OpenAPI response per **status**, so several problem
        types sharing 422 collapse to whichever is declared *last*. Declaring
        ``SETTING_BELOW_FLOOR`` after ``SETTING_ABOVE_CEILING`` silently re-described the 422 on
        every route in the published document — and therefore in the generated TypeScript client
        — as "Setting is below the server's floor". The fix is one line of ordering in
        ``problems.py``; this is the test that stops the next person undoing it.
        """
        assert _DOCUMENTED_ERRORS[422]["description"] == "Setting exceeds the server's ceiling"

    def test_the_refusal_types_are_distinct(self) -> None:
        """A refusal typed "above-ceiling" for a value that was too small is exactly the
        confusion this decision exists to prevent."""
        assert ProblemType.SETTING_BELOW_FLOOR.value == "setting-below-floor"
        assert ProblemType.SETTING_ABOVE_CEILING.value == "setting-above-ceiling"
        assert len({member.value for member in ProblemType}) == len(list(ProblemType))


class TestWhatTheFormIsTold:
    """The settings page renders "max 15 - set by the server" from :func:`to_view`.

    If the floor were handed over in the ``ceilings`` map, the page would read
    **"max 18"** for ``trail_pct`` - the exact inversion of TW0.5, rendered to the person who
    has to decide. So the two directions travel in two fields, and this is the test that says so.
    """

    @staticmethod
    def _row() -> TwConfig:
        """A detached ``tw_config`` row - the seeded defaults, no database needed."""
        return TwConfig(
            user_id=1,
            sleeve_capital_inr=Decimal("0.00"),
            max_open_positions=10,
            max_position_pct=Decimal("12.50"),
            stop_pct=Decimal("20.00"),
            trail_pct=Decimal("20.00"),
            first_live_entries_left=10,
            dry_run_sessions=0,
            updated_at=dt.datetime(2026, 9, 11, 12, 0, tzinfo=dt.UTC),
            updated_by="seed",
        )

    def test_from_settings_reads_all_four_bounds(self) -> None:
        bounds = TwtCeilings.from_settings(Settings())
        assert bounds.max_open_positions == 15
        assert bounds.max_position_pct == Decimal("15.00")
        assert bounds.stop_pct == Decimal("25.00")
        assert bounds.trail_pct_min == Decimal("18.00")

    def test_the_view_never_calls_the_floor_a_ceiling(self) -> None:
        view = to_view(self._row(), ceilings=_ceilings(), execution_enabled=False)
        assert set(view.ceilings) == {"max_open_positions", "max_position_pct", "stop_pct"}
        assert "trail_pct" not in view.ceilings
        assert view.floors == {"trail_pct": "18.00"}

    def test_the_view_shows_the_seeded_sleeve_as_having_no_capital(self) -> None:
        """`04` §9.3: what the page must be able to say on the first morning is "0"."""
        view = to_view(self._row(), ceilings=_ceilings(), execution_enabled=False)
        assert view.sleeve_capital_inr == Decimal("0.00")
        assert view.execution_enabled is False

    def test_bound_for_answers_in_both_directions_and_stays_silent_otherwise(self) -> None:
        bounds = _ceilings()
        assert bounds.bound_for("stop_pct") == Decimal("25.00")
        assert bounds.bound_for("trail_pct") == Decimal("18.00")
        assert bounds.bound_for("sleeve_capital_inr") is None


class TestTheBoundsAreTheStrategysOwnNumbers:
    def test_the_shipped_defaults_sit_inside_the_shipped_bounds(self) -> None:
        """A shipped default that already violates its own bound is a trap.

        The defaults are `04`'s and they are the ``tw_config`` column defaults: ten slots,
        12.5 % a position, a 20 % stop, a 20 % trail.
        """
        ceilings = _ceilings()
        ceilings.check("max_open_positions", 10)
        ceilings.check("max_position_pct", Decimal("12.50"))
        ceilings.check("stop_pct", Decimal("20.00"))
        ceilings.check("trail_pct", Decimal("20.00"))

    def test_the_api_and_the_worker_carry_the_same_four(self) -> None:
        api = (
            Settings.model_fields["twt_max_open_positions_max"].default,
            Settings.model_fields["twt_max_position_pct_max"].default,
            Settings.model_fields["twt_stop_pct_max"].default,
            Settings.model_fields["twt_trail_pct_min"].default,
        )
        worker = (
            WorkerSettings.model_fields["twt_max_open_positions_max"].default,
            WorkerSettings.model_fields["twt_max_position_pct_max"].default,
            WorkerSettings.model_fields["twt_stop_pct_max"].default,
            WorkerSettings.model_fields["twt_trail_pct_min"].default,
        )
        assert api == (15, Decimal("15.00"), Decimal("25.00"), Decimal("18.00"))
        assert worker == (15, 15.00, 25.00, 18.00)

    def test_raising_the_position_setting_can_never_widen_the_book_past_ten(self) -> None:
        """`04` §6.1 takes ``min(setting, max_slots)``, so a setting above the strategy's own
        slot count is a form that lies rather than a wider book. The ceiling being higher than
        the slot count is deliberate: it leaves the number visible as a setting."""
        assert STRATEGY_MAX_SLOTS == 10
        assert "`max_slots = 10`" in BUSINESS_RULES
        assert Settings.model_fields["twt_max_open_positions_max"].default > STRATEGY_MAX_SLOTS

    def test_the_stop_ceiling_is_the_top_of_the_measured_plateau(self) -> None:
        """`01` §7 measured 15-30 % and found the band flat, which is why the stop is a setting
        at all; `02` §4 puts the ceiling at 25."""
        assert Settings.model_fields["twt_stop_pct_max"].default == Decimal("25.00")
        assert "BASKFY_TWT_STOP_PCT_MAX` [25.00]" in SCOPE


class TestTheEnvironmentExamplesShipThem:
    @pytest.mark.parametrize("path", ENV_EXAMPLES, ids=lambda p: str(p.name))
    @pytest.mark.parametrize(
        ("assignment"),
        [
            "BASKFY_TWT_MAX_OPEN_POSITIONS_MAX=15",
            "BASKFY_TWT_MAX_POSITION_PCT_MAX=15.00",
            "BASKFY_TWT_STOP_PCT_MAX=25.00",
            "BASKFY_TWT_TRAIL_PCT_MIN=18.00",
        ],
    )
    def test_every_bound_is_in_every_committed_example(self, path: Path, assignment: str) -> None:
        assert assignment in path.read_text(encoding="utf-8")

    @pytest.mark.parametrize("path", ENV_EXAMPLES, ids=lambda p: str(p.name))
    def test_the_example_says_out_loud_that_one_of_them_is_a_floor(self, path: Path) -> None:
        """The comment is the thing a person reads at 3am, and the one this run must not get
        wrong. ``02`` §4's own sentence, in the file that ships."""
        text = path.read_text(encoding="utf-8")
        assert "A FLOOR, NOT A CEILING" in text
        assert "DECISIONS-TW TW0.5" in text

    @pytest.mark.parametrize("path", ENV_EXAMPLES, ids=lambda p: str(p.name))
    def test_no_committed_example_assigns_a_capital(self, path: Path) -> None:
        """``sleeve_capital_inr`` is not an environment variable, is seeded at 0, and is
        Maulik's to enter on the first live morning. A committed file that carried a number
        would be an engineering path around that (`02` §3).

        The check is on *assignments*, not on the word: the examples mention the field in prose
        precisely so a reader knows why it is absent, and a test that banned the word would push
        that explanation out of the file.
        """
        assignments = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if "sleeve_capital_inr" in line.split("#", 1)[0] and "=" in line.split("#", 1)[0]
        ]
        assert assignments == [], assignments


class TestTheFlagsDefaultOff:
    """Track B: "built dark, flag-off". The default is where that lives."""

    def test_api_settings_default_execution_false(self) -> None:
        assert Settings.model_fields["twt_execution_enabled"].default is False

    def test_worker_settings_default_execution_false(self) -> None:
        assert WorkerSettings.model_fields["twt_execution_enabled"].default is False

    def test_detection_defaults_on_because_it_moves_no_money(self) -> None:
        """`02` Track B — and a sleeve with no history is a sleeve with no evidence."""
        assert Settings.model_fields["twt_nightly_enabled"].default is True
        assert WorkerSettings.model_fields["twt_nightly_enabled"].default is True

    def test_this_deployment_reads_execution_as_false_too(self) -> None:
        """And the constructed settings agree, on this machine, with this `.env`."""
        assert Settings().twt_execution_enabled is False

    def test_no_auto_execute_setting_exists_anywhere(self) -> None:
        """`02` Track B and Track C §3. Non-negotiable 1's named exception belongs to the swing
        sleeve, by Maulik's own hand; this run neither widens it nor adds a second one. In
        particular the ratchet — which will want to fire on ten lines a night, for months — is a
        plan line a person confirms, never a background job that talks to the broker."""
        offenders = [
            name
            for name in (*Settings.model_fields, *WorkerSettings.model_fields)
            if name.startswith("twt_") and "auto" in name
        ]
        assert offenders == []

    def test_no_committed_environment_file_enables_twt_execution(self) -> None:
        """The same scan Prompt 20 §2 runs over ``BASKFY_PUBLIC_API_ENABLED``.

        A committed ``.env.example`` that turned it on would be an engineering path around `02`
        §3's gate, because a new deployment copies that file verbatim. ``NIGHTLY`` is allowed to
        be true: it detects, and detection places nothing.
        """
        offenders: list[str] = []
        for path in sorted(MONOREPO_ROOT.rglob(".env*")):
            if any(part in {"node_modules", ".git", ".venv"} for part in path.parts):
                continue
            if not path.is_file():
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.split("#", 1)[0].strip()
                if not stripped.startswith("BASKFY_TWT_"):
                    continue
                if "NIGHTLY" in stripped:
                    continue
                if stripped.lower().endswith("=true"):
                    offenders.append(f"{path.relative_to(MONOREPO_ROOT)}:{number}: {stripped}")
        assert offenders == [], f"a TWT flag is enabled in a committed env file: {offenders}"


class TestThePatchShape:
    @pytest.mark.parametrize("field", sorted(SYSTEM_OWNED_FIELDS))
    def test_a_system_owned_field_cannot_be_patched(self, field: str) -> None:
        """``first_live_entries_left`` is `04` §6.4's countdown; a person who could set it has
        deleted the half-size discipline. ``dry_run_sessions`` is the evening's own count."""
        with pytest.raises(ValueError, match=r"extra_forbidden|Extra inputs"):
            TwtConfigPatch.model_validate({field: 0})

    def test_an_unknown_field_is_refused_rather_than_ignored(self) -> None:
        """Silently dropping a field the caller sent is how a settings page comes to believe it
        saved something it did not."""
        with pytest.raises(ValueError, match=r"extra_forbidden|Extra inputs"):
            TwtConfigPatch.model_validate({"tight_band_pct": 4.0})

    def test_the_editable_fields_are_the_documents_five(self) -> None:
        """`02` §4 — everything else in `04` is code, with a DECISIONS-TW entry."""
        assert EDITABLE_FIELDS == (
            "sleeve_capital_inr",
            "max_open_positions",
            "max_position_pct",
            "stop_pct",
            "trail_pct",
        )

    def test_an_empty_patch_changes_nothing(self) -> None:
        assert TwtConfigPatch().changes() == {}

    def test_only_the_fields_the_caller_set_are_changes(self) -> None:
        patch = TwtConfigPatch.model_validate({"trail_pct": "22.00"})
        assert patch.changes() == {"trail_pct": Decimal("22.00")}

    @pytest.mark.parametrize("value", [0, -1])
    def test_a_nonsense_trail_is_a_four_hundred_not_a_floor_question(self, value: int) -> None:
        """The engine's limits and the server's bounds are different questions with different
        answers: nonsense is a 400 here, and "below the floor" is a 422 in ``apply_patch``."""
        with pytest.raises(ValueError, match=r"greater_than|Input should be"):
            TwtConfigPatch.model_validate({"trail_pct": value})
