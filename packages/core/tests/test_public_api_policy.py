"""The public API's compliance policy — Prompt 20 deliverable 2, and its third acceptance criterion.

    docs/11 §"Compliance & legal (India)": "Serve derived analytics; do not expose a raw-bar API
    to third parties without written clearance. Get a written data-redistribution opinion before
    enabling the public API tier."

These tests assert the *document*, not the code's convenience:

* the review is not signed off, and therefore the feature is blocked;
* no field denominated in rupees per share is in the served set;
* every column of docs/01 §4's picker is either served or explicitly withheld, so a column added
  later cannot slip into the public API by not being thought about;
* the terms-of-use document names both lists, so a caller can see what is withheld.

``decile_core.api_keys`` is asserted here too, because the property that matters about it — every
scope is a read — is a policy statement rather than a behaviour.
"""

from __future__ import annotations

import pytest

from decile_core.api_keys import (
    DEFAULT_SCOPES,
    KEY_TOKEN_MARKER,
    InvalidKeyFormat,
    Scope,
    format_key,
    normalise_scopes,
    parse_key,
    redact,
)
from decile_core.factor_registry import COLUMN_PICKER_KEYS, FactorUnit, columns
from decile_core.public_api import (
    DATA_REDISTRIBUTION_REVIEW,
    PUBLIC_API_PREFIX,
    PUBLIC_API_VERSION,
    PUBLIC_COLUMNS,
    PUBLIC_SORT_FACTORS,
    RAW_BAR_FIELDS,
    WITHHELD_COLUMNS,
    is_public_column,
    is_public_sort,
    terms_of_use,
)


class TestTheReviewGate:
    def test_the_data_redistribution_review_is_not_signed_off(self) -> None:
        """**If this fails, someone flipped the compliance gate.**

        docs/11 requires a written opinion first. The constant is a source value precisely so
        that turning it on is a reviewable commit rather than an environment variable — and this
        test is what makes the commit impossible to make quietly.
        """
        assert DATA_REDISTRIBUTION_REVIEW.signed_off is False
        assert DATA_REDISTRIBUTION_REVIEW.blocks_public_api is True
        assert DATA_REDISTRIBUTION_REVIEW.opinion_reference == ""

    def test_the_requirement_quotes_docs_11(self) -> None:
        requirement = DATA_REDISTRIBUTION_REVIEW.requirement
        assert "written data-redistribution opinion" in requirement
        assert "docs/11" in requirement


class TestTheColumnWhitelist:
    def test_no_served_column_is_priced_in_rupees_per_share(self) -> None:
        units = {column.key: column.unit for column in columns()}
        offenders = [key for key in PUBLIC_COLUMNS if units[key] is FactorUnit.PRICE]
        assert offenders == []

    @pytest.mark.parametrize(
        "key", ["close", "close_raw", "high_1y", "high_ath", "ma_200", "ma_100", "ma_50", "ma_20"]
    )
    def test_the_price_columns_are_withheld(self, key: str) -> None:
        assert key not in PUBLIC_COLUMNS
        assert key in WITHHELD_COLUMNS

    @pytest.mark.parametrize("key", ["ret_12m", "sharpe_12m", "vol_12m", "beta_12m", "rsi_12m"])
    def test_the_derived_analytics_are_served(self, key: str) -> None:
        assert key in PUBLIC_COLUMNS

    def test_every_picker_column_is_decided_one_way_or_the_other(self) -> None:
        """A column added to docs/01 §4 later must land in one list or the other, never neither."""
        assert set(PUBLIC_COLUMNS) | set(WITHHELD_COLUMNS) == set(COLUMN_PICKER_KEYS)
        assert set(PUBLIC_COLUMNS) & set(WITHHELD_COLUMNS) == set()

    def test_no_raw_bar_field_is_ever_public(self) -> None:
        assert set(PUBLIC_COLUMNS) & RAW_BAR_FIELDS == set()

    def test_the_rule_refuses_a_raw_bar_field_whatever_unit_it_claims(self) -> None:
        """The name check is belt to the unit rule's braces: ``volume`` is a count, not a price."""
        assert is_public_column("volume", FactorUnit.COUNT) is False
        assert is_public_column("close_raw", FactorUnit.RATIO) is False


class TestTheSortWhitelist:
    """A sort is judged against the *registry*, not against docs/01 §4's column picker."""

    def test_a_blend_may_be_sorted_by(self) -> None:
        """`avg_sharpe_12_6_3_1` is what docs/13's captured export sorts by, and is derived."""
        assert is_public_sort("avg_sharpe_12_6_3_1")

    @pytest.mark.parametrize("key", ["close", "close_raw"])
    def test_a_price_may_not_be_sorted_by(self, key: str) -> None:
        assert not is_public_sort(key)

    def test_every_public_column_that_is_a_factor_is_also_sortable(self) -> None:
        factors = {column.key for column in columns() if column.is_factor}
        assert {key for key in PUBLIC_COLUMNS if key in factors} <= PUBLIC_SORT_FACTORS


class TestTheTermsDocument:
    def test_it_names_what_is_served_and_what_is_withheld(self) -> None:
        document = terms_of_use(contact="api@example.com")
        assert document.served_fields == PUBLIC_COLUMNS
        assert document.withheld_fields == WITHHELD_COLUMNS

    def test_it_is_machine_readable(self) -> None:
        """A caller's *code* has to be able to act on it, not only a caller's lawyer."""
        payload = terms_of_use(contact="api@example.com").as_dict()
        assert payload["redistribution_permitted"] is False
        assert payload["attribution_required"] is True
        assert isinstance(payload["caching_max_age_seconds"], int)
        assert isinstance(payload["clauses"], list)

    def test_it_carries_the_sebi_disclaimer(self) -> None:
        """docs/11 §Compliance: the disclaimer belongs on every analytics surface."""
        disclaimer = terms_of_use(contact="api@example.com").disclaimer
        assert "not a SEBI-registered investment adviser" in disclaimer
        assert "not investment advice" in disclaimer

    def test_it_prohibits_reconstructing_a_price_series(self) -> None:
        prohibited = " ".join(terms_of_use(contact="api@example.com").prohibited_use).lower()
        assert "reconstruction" in prohibited


class TestVersioning:
    def test_the_public_prefix_is_versioned_separately_from_the_product_api(self) -> None:
        assert f"/api/public/{PUBLIC_API_VERSION}" == PUBLIC_API_PREFIX
        assert PUBLIC_API_PREFIX != "/api/v1"


class TestScopesAreReadOnly:
    def test_every_scope_is_a_read(self) -> None:
        """The enforcement is the absence of anything to request: a write scope fails here."""
        assert all(scope.is_read_only for scope in Scope)
        assert all(scope.value.endswith(":read") for scope in Scope)

    def test_the_default_is_every_scope(self) -> None:
        assert normalise_scopes(None) == DEFAULT_SCOPES
        assert set(DEFAULT_SCOPES) == set(Scope)

    def test_an_unknown_scope_is_an_error_not_a_silent_drop(self) -> None:
        with pytest.raises(ValueError, match="unknown scope"):
            normalise_scopes(["screens:write"])

    def test_an_empty_scope_list_is_refused(self) -> None:
        with pytest.raises(ValueError, match="no scopes"):
            normalise_scopes([])

    def test_scopes_come_back_in_a_fixed_order(self) -> None:
        assert normalise_scopes(["meta:read", "screens:read"]) == normalise_scopes(
            ["screens:read", "meta:read"]
        )


class TestKeyFormat:
    def test_a_secret_containing_an_underscore_round_trips(self) -> None:
        """``token_urlsafe`` emits ``_``; splitting on every one rejected half of all keys."""
        secret = "w0lwTwZrTKnAuPLcZ7N4kLAatH_UBBGQQUcWnSxV81U"
        parsed = parse_key(format_key("7f3a9c1b4d2e", secret))
        assert parsed.secret == secret

    def test_a_key_round_trips(self) -> None:
        presented = format_key("7f3a9c1b4d2e", "a" * 43)
        parsed = parse_key(presented)
        assert parsed.prefix == "7f3a9c1b4d2e"
        assert parsed.secret == "a" * 43
        assert parsed.display == f"{KEY_TOKEN_MARKER}_7f3a9c1b4d2e"

    @pytest.mark.parametrize(
        "presented",
        [
            "",
            "not-a-key",
            "rzp_7f3a9c1b4d2e_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "dk_ZZZZZZZZZZZZ_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "dk_7f3a9c1b4d2e_short",
            "dk_7f3a9c1b4d2e",
        ],
    )
    def test_a_malformed_key_is_refused_before_any_lookup(self, presented: str) -> None:
        with pytest.raises(InvalidKeyFormat):
            parse_key(presented)

    def test_redaction_keeps_the_prefix_and_loses_the_secret(self) -> None:
        secret = "s3cr3t" * 8
        presented = format_key("7f3a9c1b4d2e", secret)
        redacted = redact(presented)
        assert secret not in redacted
        assert "7f3a9c1b4d2e" in redacted

    def test_redacting_something_that_is_not_a_key_reveals_nothing(self) -> None:
        assert redact("Bearer eyJhbGciOi") == "<redacted>"
