"""Retry policy and encrypted token storage (Prompt 2 deliverable 2).

docs/09 §"Kite specifics": the access token is daily, must be stored encrypted, and its expiry
must "alert loudly" — it is "the #1 pipeline failure".
docs/11 §Security: "Kite access token encrypted at rest."
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from decile_providers.errors import (
    AccessTokenExpired,
    CredentialsMissing,
    RetryBudgetExhausted,
    UnexpectedPayload,
    UpstreamUnavailable,
)
from decile_providers.retry import RetryHooks, RetryPolicy, call_with_retry
from decile_providers.tokens import IST, AccessToken, AccessTokenStore

ISSUED = dt.datetime(2026, 8, 20, 9, 15, tzinfo=IST)


@pytest.fixture
def store(tmp_path: Path) -> AccessTokenStore:
    return AccessTokenStore(tmp_path / "kite.enc", Fernet.generate_key().decode())


class TestRetryPolicy:
    def test_backoff_doubles(self) -> None:
        policy = RetryPolicy(base_seconds=0.5, max_seconds=30)
        assert [policy.backoff_ceiling(n) for n in (1, 2, 3, 4)] == [0.5, 1.0, 2.0, 4.0]

    def test_backoff_is_capped(self) -> None:
        """Without a cap, attempt 12 would sleep for an hour and the run would miss its window."""
        policy = RetryPolicy(base_seconds=1.0, max_seconds=8.0)
        assert policy.backoff_ceiling(10) == 8.0

    def test_attempts_are_one_based(self) -> None:
        with pytest.raises(ValueError, match="1-based"):
            RetryPolicy().backoff_ceiling(0)

    def test_configuration_is_validated(self) -> None:
        with pytest.raises(ValueError, match="max_attempts"):
            RetryPolicy(max_attempts=0)
        with pytest.raises(ValueError, match="base_seconds"):
            RetryPolicy(base_seconds=0)
        with pytest.raises(ValueError, match="max_seconds"):
            RetryPolicy(base_seconds=10, max_seconds=1)


class TestJitter:
    def test_the_default_jitter_is_full_not_symmetric(self) -> None:
        """Full jitter spreads N workers across the whole interval; +/- keeps them synchronised
        and they retry as a thundering herd."""
        draws = [RetryHooks().draw(4.0) for _ in range(200)]
        assert all(0.0 <= d <= 4.0 for d in draws)
        assert min(draws) < 1.0
        assert max(draws) > 3.0

    def test_jitter_is_injectable_for_deterministic_tests(self) -> None:
        assert RetryHooks(jitter=lambda ceiling: ceiling / 2).draw(4.0) == 2.0


class TestRetryLoop:
    def test_a_success_is_returned_without_sleeping(self) -> None:
        sleeps: list[float] = []
        result = call_with_retry(
            lambda: "ok", RetryPolicy(), hooks=RetryHooks(sleeper=sleeps.append)
        )
        assert result == "ok"
        assert sleeps == []

    def test_it_stops_at_max_attempts(self) -> None:
        calls = 0

        def flaky() -> None:
            nonlocal calls
            calls += 1
            raise UpstreamUnavailable("503")

        with pytest.raises(RetryBudgetExhausted):
            call_with_retry(
                flaky,
                RetryPolicy(max_attempts=3),
                hooks=RetryHooks(sleeper=lambda _: None, jitter=lambda c: c),
            )
        assert calls == 3

    def test_the_final_error_is_carried(self) -> None:
        """ "We tried 5 times" without "here is what broke" is not an operable message."""
        with pytest.raises(RetryBudgetExhausted) as raised:
            call_with_retry(
                _fail,
                RetryPolicy(max_attempts=2),
                provider="kite",
                hooks=RetryHooks(sleeper=lambda _: None),
            )
        assert isinstance(raised.value.last_error, UpstreamUnavailable)
        assert raised.value.attempts == 2
        assert "kite" in str(raised.value)

    def test_a_non_transient_error_is_not_retried(self) -> None:
        calls = 0

        def broken() -> None:
            nonlocal calls
            calls += 1
            raise CredentialsMissing("no key")

        with pytest.raises(CredentialsMissing):
            call_with_retry(broken, RetryPolicy(max_attempts=5))
        assert calls == 1

    def test_the_retry_hook_observes_each_attempt(self) -> None:
        """Prompt 17 hangs OpenTelemetry spans and the provider error-rate metric here."""
        observed: list[tuple[int, float]] = []
        with pytest.raises(RetryBudgetExhausted):
            call_with_retry(
                _fail,
                RetryPolicy(max_attempts=3, base_seconds=1.0),
                hooks=RetryHooks(
                    sleeper=lambda _: None,
                    jitter=lambda c: c,
                    on_retry=lambda attempt, delay, _e: observed.append((attempt, delay)),
                ),
            )
        assert observed == [(1, 1.0), (2, 2.0)]

    def test_a_single_attempt_policy_never_sleeps(self) -> None:
        sleeps: list[float] = []
        with pytest.raises(RetryBudgetExhausted):
            call_with_retry(
                _fail, RetryPolicy(max_attempts=1), hooks=RetryHooks(sleeper=sleeps.append)
            )
        assert sleeps == []


class TestTokenExpiry:
    def test_a_token_issued_today_is_fresh(self) -> None:
        token = AccessToken("abc", ISSUED)
        assert not token.is_expired(now=ISSUED + dt.timedelta(hours=14))

    def test_a_token_is_expired_once_the_ist_day_rolls_over(self) -> None:
        """Kite invalidates at the next trading day's pre-open, not N hours after issue."""
        token = AccessToken("abc", dt.datetime(2026, 8, 20, 23, 55, tzinfo=IST))
        assert token.is_expired(now=dt.datetime(2026, 8, 21, 0, 5, tzinfo=IST))

    def test_expiry_is_judged_in_ist_not_utc(self) -> None:
        """A UTC-based check would call a token fresh for 5.5 hours after it died."""
        issued = dt.datetime(2026, 8, 20, 20, 0, tzinfo=IST)  # 14:30 UTC
        later = dt.datetime(2026, 8, 21, 1, 0, tzinfo=IST)  # 19:30 UTC the same UTC day
        assert AccessToken("abc", issued).is_expired(now=later)


class TestTokenStore:
    def test_round_trip(self, store: AccessTokenStore) -> None:
        store.save("secret-token", issued_at=ISSUED)
        assert store.load().value == "secret-token"

    def test_the_file_is_encrypted_at_rest(self, store: AccessTokenStore) -> None:
        """docs/11: "Kite access token encrypted at rest"."""
        store.save("secret-token", issued_at=ISSUED)
        assert b"secret-token" not in store.path.read_bytes()

    def test_the_file_is_not_world_readable(self, store: AccessTokenStore) -> None:
        store.save("secret-token", issued_at=ISSUED)
        assert store.path.stat().st_mode & 0o777 == 0o600

    def test_require_fresh_returns_a_same_day_token(self, store: AccessTokenStore) -> None:
        store.save("secret-token", issued_at=ISSUED)
        assert store.require_fresh(now=ISSUED + dt.timedelta(hours=6)).value == "secret-token"

    def test_require_fresh_raises_loudly_once_stale(self, store: AccessTokenStore) -> None:
        """docs/09 wants a human paged; returning a dead token just moves the failure."""
        store.save("secret-token", issued_at=ISSUED)
        with pytest.raises(AccessTokenExpired) as raised:
            store.require_fresh(now=ISSUED + dt.timedelta(days=1))
        assert "login flow" in str(raised.value)

    def test_a_missing_file_is_credentials_missing(self, store: AccessTokenStore) -> None:
        with pytest.raises(CredentialsMissing, match="no Kite access token"):
            store.load()

    def test_an_empty_token_is_refused(self, store: AccessTokenStore) -> None:
        with pytest.raises(ValueError, match="empty"):
            store.save("")

    def test_no_encryption_key_is_credentials_missing(self, tmp_path: Path) -> None:
        with pytest.raises(CredentialsMissing, match="ENCRYPTION_KEY"):
            AccessTokenStore(tmp_path / "k.enc", "").save("abc")

    def test_an_invalid_encryption_key_is_reported_clearly(self, tmp_path: Path) -> None:
        with pytest.raises(CredentialsMissing, match="valid Fernet key"):
            AccessTokenStore(tmp_path / "k.enc", "not-a-fernet-key").save("abc")

    def test_a_rotated_key_is_diagnosed_rather_than_crashing(self, tmp_path: Path) -> None:
        """The realistic operational failure: someone rotated the secret and forgot the token."""
        path = tmp_path / "k.enc"
        AccessTokenStore(path, Fernet.generate_key().decode()).save("abc", issued_at=ISSUED)
        with pytest.raises(CredentialsMissing, match="rotated"):
            AccessTokenStore(path, Fernet.generate_key().decode()).load()

    def test_a_corrupt_payload_is_reported_as_such(self, tmp_path: Path) -> None:
        key = Fernet.generate_key().decode()
        path = tmp_path / "k.enc"
        path.write_bytes(Fernet(key.encode()).encrypt(b"this is not json"))
        with pytest.raises(UnexpectedPayload):
            AccessTokenStore(path, key).load()


def _fail() -> None:
    raise UpstreamUnavailable("503")
