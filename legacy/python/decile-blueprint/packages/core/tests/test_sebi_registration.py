"""SEBI registration capture, asserted as a specification.

The class that matters most here is ``TestWhatThisDoesNotClaim``. The module's whole risk is that
a reader — or a future function — treats "the number looks right" as "this person is allowed to
manage money". Those tests are the standing objection to that.
"""

from __future__ import annotations

import datetime as dt
import inspect
from pathlib import Path

import pytest

from baskfy_core import sebi_registration as sr
from baskfy_core.sebi_registration import (
    REGISTRATION_PATTERNS,
    REGISTRATION_TYPES,
    FormatVerdict,
    check_format,
    is_current,
    normalise,
)

JAN_2024 = dt.date(2024, 1, 1)
TODAY = dt.date(2026, 8, 25)


class TestFormatChecking:
    @pytest.mark.parametrize(
        ("kind", "number"),
        [
            ("RESEARCH_ANALYST", "INH000001234"),
            ("INVESTMENT_ADVISER", "INA000001234"),
            ("PORTFOLIO_MANAGER", "INP000001234"),
            ("AIF", "IN/AIF2/20-21/0123"),
            ("MF_DISTRIBUTOR", "ARN-12345"),
        ],
    )
    def test_a_well_formed_number_is_recognised(self, kind: str, number: str) -> None:
        verdict = check_format(kind, number)
        assert verdict.well_formed is True
        assert verdict.registration_type == kind
        assert verdict.normalised == number.upper()

    def test_a_number_declared_as_the_wrong_type_is_caught_and_says_which(self) -> None:
        """An INA number filed as a research-analyst registration is a real, common mistake."""
        verdict = check_format("RESEARCH_ANALYST", "INA000001234")
        assert verdict.well_formed is False
        assert verdict.registration_type == "UNKNOWN"
        assert "INVESTMENT_ADVISER" in verdict.reason

    def test_an_unrecognised_shape_is_kept_for_review_not_thrown_away(self) -> None:
        """A manager holding a shape we have not seen must still be able to apply."""
        verdict = check_format("RESEARCH_ANALYST", "some-new-shape-2029")
        assert verdict.registration_type == "UNKNOWN"
        assert verdict.well_formed is False
        assert "review" in verdict.reason
        assert verdict.normalised, "the number itself must survive for a human to look at"

    def test_declaring_none_with_a_number_is_refused(self) -> None:
        with pytest.raises(ValueError, match="cannot carry a number"):
            check_format("NONE", "INH000001234")

    def test_declaring_a_type_without_a_number_is_refused(self) -> None:
        for empty in (None, "", "   "):
            with pytest.raises(ValueError, match="requires a number"):
                check_format("RESEARCH_ANALYST", empty)

    def test_none_is_a_real_answer_not_an_error(self) -> None:
        """An operator-curated basket that is not advice may legitimately have no registration."""
        verdict = check_format("NONE", None)
        assert verdict.registration_type == "NONE"
        assert verdict.well_formed is False
        assert "no registration" in verdict.reason

    def test_an_unknown_registration_type_is_refused(self) -> None:
        with pytest.raises(ValueError, match="is not a registration type"):
            check_format("BANKER", "INH000001234")

    @pytest.mark.parametrize(
        "bad",
        ["INH00000123", "INH0000012345", "INHX00001234", "INH 000001234X", "ARN-", "ARN-1234567"],
    )
    def test_near_misses_do_not_pass(self, bad: str) -> None:
        """Off-by-one digit counts are the whole reason a pattern check earns its place."""
        assert check_format("RESEARCH_ANALYST", bad).well_formed is False


class TestNormalisation:
    def test_case_and_whitespace_do_not_change_the_registration(self) -> None:
        assert normalise("  inh 000001234 ") == "INH000001234"

    def test_a_number_copied_off_a_certificate_still_matches(self) -> None:
        assert check_format("RESEARCH_ANALYST", " inh 000 001 234 ").well_formed is True

    def test_storage_and_comparison_agree(self) -> None:
        """Two spellings of one registration must not become two registrations."""
        a = check_format("RESEARCH_ANALYST", "inh000001234").normalised
        b = check_format("RESEARCH_ANALYST", "INH 000001234").normalised
        assert a == b


class TestValidityWindow:
    def test_an_open_ended_registration_is_current(self) -> None:
        assert is_current(valid_from=JAN_2024, valid_to=None, as_of=TODAY) is True

    def test_a_lapsed_registration_is_not_current(self) -> None:
        assert is_current(valid_from=JAN_2024, valid_to=dt.date(2025, 1, 1), as_of=TODAY) is False

    def test_a_registration_that_has_not_started_is_not_current(self) -> None:
        assert is_current(valid_from=dt.date(2027, 1, 1), valid_to=None, as_of=TODAY) is False

    def test_the_boundary_days_are_inclusive(self) -> None:
        assert is_current(valid_from=TODAY, valid_to=TODAY, as_of=TODAY) is True

    def test_a_missing_start_date_is_not_treated_as_the_dawn_of_time(self) -> None:
        """With no window there is no answer; somebody must supply a start date."""
        assert is_current(valid_from=None, valid_to=None, as_of=TODAY) is False

    def test_as_of_is_required_so_a_job_and_a_request_cannot_disagree(self) -> None:
        """No default means no hidden clock read — law 1, and determinism.

        Asserted against the signature rather than by calling with the argument missing: that
        call needs a suppression comment to get past the type checker, and house rule 3 forbids
        those. The signature is the stronger claim anyway — it says the parameter can never
        acquire a default, not merely that today's call fails.
        """
        parameter = inspect.signature(is_current).parameters["as_of"]
        assert parameter.default is inspect.Parameter.empty, "as_of must never gain a default"
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


class TestWhatThisDoesNotClaim:
    def test_the_module_never_marks_anything_verified(self) -> None:
        """Verification is an operator comparing against the SEBI register, not a regex."""
        source = sr.__file__ or ""
        assert source
        text = Path(source).read_text(encoding="utf-8")
        assert "sebi_reg_verified_at" not in text.split('"""', 2)[-1], (
            "the pure module must not write the verification column"
        )

    def test_a_well_formed_number_is_not_called_valid(self) -> None:
        """Naming is the defence here: 'valid' invites the wrong belief."""
        assert not hasattr(FormatVerdict("NONE", "", False, "x"), "valid")
        assert hasattr(FormatVerdict("NONE", "", False, "x"), "well_formed")

    def test_a_well_formed_number_says_nothing_about_currency(self) -> None:
        """Well-formed and current are independent facts and must stay independent."""
        assert check_format("RESEARCH_ANALYST", "INH000001234").well_formed is True
        assert is_current(valid_from=None, valid_to=None, as_of=TODAY) is False

    def test_no_registration_type_asserts_compliance(self) -> None:
        """No value in the vocabulary may read as a permission to manage money."""
        forbidden = {"COMPLIANT", "APPROVED", "AUTHORISED", "AUTHORIZED", "PERMITTED", "VALID"}
        assert forbidden.isdisjoint(set(REGISTRATION_TYPES))

    def test_the_disclaimer_survives_in_the_module_docstring(self) -> None:
        """D3 is unreviewed; the file must keep saying so where a reader will meet it."""
        doc = sr.__doc__ or ""
        assert "does not mean" in doc or "does NOT claim" in doc
        assert "D3" in doc

    def test_unverified_is_the_only_possible_starting_point(self) -> None:
        """Nothing this module returns can be mistaken for a verification record."""
        verdict = check_format("RESEARCH_ANALYST", "INH000001234")
        fields = set(verdict.__dataclass_fields__)
        assert not fields & {"verified", "verified_at", "compliant", "approved"}


class TestTheVocabularyIsClosed:
    def test_every_pattern_names_a_declared_type(self) -> None:
        assert set(REGISTRATION_PATTERNS) <= set(REGISTRATION_TYPES)

    def test_none_and_unknown_carry_no_pattern(self) -> None:
        """They are answers about the absence of a match, so a pattern would be incoherent."""
        assert "NONE" not in REGISTRATION_PATTERNS
        assert "UNKNOWN" not in REGISTRATION_PATTERNS

    def test_a_verdict_cannot_be_constructed_claiming_none_is_well_formed(self) -> None:
        with pytest.raises(ValueError, match="never well-formed"):
            FormatVerdict("NONE", "", True, "x")
        with pytest.raises(ValueError, match="never well-formed"):
            FormatVerdict("UNKNOWN", "X", True, "x")

    def test_a_verdict_cannot_hold_an_invented_type(self) -> None:
        with pytest.raises(ValueError, match="is not a registration type"):
            FormatVerdict("BANKER", "X", False, "x")

    def test_every_pattern_is_anchored(self) -> None:
        """An unanchored pattern would accept a valid number buried in junk."""
        for name, pattern in REGISTRATION_PATTERNS.items():
            assert pattern.pattern.startswith("^"), f"{name} is not anchored at the start"
            assert pattern.pattern.endswith("$"), f"{name} is not anchored at the end"
