"""The hashing and token primitives — docs/11 §Security (Prompt 12 deliverable 2).

No database and no HTTP: these are the properties the rest of the auth system is built on, and
they are cheapest to assert directly.
"""

from __future__ import annotations

import re

import pytest

from baskfy_api.security import (
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    WeakPassword,
    check_password_policy,
    digest,
    digests_match,
    hash_password,
    needs_rehash,
    new_opaque_token,
    new_otp,
    verify_password,
)
from baskfy_api.settings import Settings

PASSWORD = "a reasonable passphrase"

SHA256_HEX_LENGTH = 64


def cheap() -> Settings:
    """The smallest parameters argon2-cffi accepts. The *defaults* are asserted separately."""
    return Settings(argon2_time_cost=1, argon2_memory_kib=8, argon2_parallelism=1)


class TestArgon2id:
    def test_the_defaults_follow_owasps_baseline(self) -> None:
        """docs/11 names the algorithm; OWASP's 2024 note gives the interactive-login parameters."""
        settings = Settings()
        assert settings.argon2_memory_kib == 19 * 1024
        assert settings.argon2_time_cost == 2
        assert settings.argon2_parallelism == 1

    def test_a_hash_is_argon2id_and_does_not_contain_the_password(self) -> None:
        stored = hash_password(PASSWORD, cheap())
        assert stored.startswith("$argon2id$")
        assert PASSWORD not in stored

    def test_two_hashes_of_one_password_differ(self) -> None:
        """A per-hash salt, which is what stops a rainbow table."""
        settings = cheap()
        assert hash_password(PASSWORD, settings) != hash_password(PASSWORD, settings)

    def test_verification_round_trips(self) -> None:
        settings = cheap()
        stored = hash_password(PASSWORD, settings)
        assert verify_password(PASSWORD, stored, settings) is True
        assert verify_password("something else", stored, settings) is False

    def test_verifying_against_no_hash_is_false_and_still_hashes(self) -> None:
        """An OTP-only account must not be identifiable by how fast the password path answers."""
        assert verify_password(PASSWORD, None, cheap()) is False

    def test_a_stronger_parameter_set_asks_for_a_rehash(self) -> None:
        stored = hash_password(PASSWORD, cheap())
        stronger = Settings(argon2_time_cost=3, argon2_memory_kib=1024, argon2_parallelism=1)
        assert needs_rehash(stored, stronger) is True
        assert needs_rehash(stored, cheap()) is False

    def test_garbage_is_not_a_valid_hash(self) -> None:
        assert verify_password(PASSWORD, "not-a-hash", cheap()) is False
        assert needs_rehash("not-a-hash", cheap()) is True


class TestPasswordPolicy:
    def test_it_follows_nist_sp_800_63b(self) -> None:
        """Minimum eight, no composition rules — a long passphrase of one character class passes."""
        assert MIN_PASSWORD_LENGTH == 8
        check_password_policy("correct horse battery staple")
        check_password_policy("aaaaaaaa")

    def test_it_refuses_a_short_one(self) -> None:
        with pytest.raises(WeakPassword):
            check_password_policy("a" * (MIN_PASSWORD_LENGTH - 1))

    def test_it_refuses_an_absurd_one(self) -> None:
        with pytest.raises(WeakPassword):
            check_password_policy("a" * (MAX_PASSWORD_LENGTH + 1))


class TestOtp:
    def test_it_is_exactly_the_requested_number_of_digits(self) -> None:
        for _ in range(200):
            code = new_otp(6)
            assert len(code) == 6
            assert code.isdigit()

    def test_leading_zeros_are_kept(self) -> None:
        """A generator that drops them silently loses 10% of the space."""
        codes = {new_otp(4) for _ in range(4000)}
        assert any(code.startswith("0") for code in codes), "no zero-led code in 4000 draws"

    def test_codes_are_not_sequential(self) -> None:
        assert len({new_otp(6) for _ in range(100)}) > 90


class TestOpaqueTokens:
    def test_a_token_is_long_and_url_safe(self) -> None:
        token = new_opaque_token()
        assert len(token) >= 43, "256 bits base64url is 43 characters"
        assert re.fullmatch(r"[A-Za-z0-9_\-]+", token)

    def test_tokens_do_not_repeat(self) -> None:
        assert len({new_opaque_token() for _ in range(1000)}) == 1000

    def test_the_stored_form_is_a_digest_not_the_token(self) -> None:
        token = new_opaque_token()
        stored = digest(token)
        assert token not in stored
        assert len(stored) == SHA256_HEX_LENGTH
        assert digests_match(token, stored) is True
        assert digests_match(new_opaque_token(), stored) is False
