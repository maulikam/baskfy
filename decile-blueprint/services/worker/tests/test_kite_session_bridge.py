"""The bridge around one redirect URL — `baskfy_worker.kite_session_cli`.

A Kite Connect app has exactly one registered redirect URL, and the RENIL app's points at the
momentum desk, which places live orders and cannot lose its login. Baskfy therefore can never
*start* a Kite login. It does not need to: `KiteProvider` performs no OAuth, it reads an access
token out of `AccessTokenStore`. The desk obtains that token every morning through the redirect it
owns; this carries it to where Baskfy looks.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from baskfy_providers.settings import get_provider_settings
from baskfy_providers.tokens import AccessTokenStore
from baskfy_worker import kite_session_cli


def _reset_settings_cache() -> None:
    """Drop the `@lru_cache` on `get_provider_settings` so a monkeypatched env is seen.

    A helper rather than a suppression at each call site. `lru_cache` gives the wrapped function a
    `cache_clear` attribute at runtime that the type checker cannot see on a bare `Callable`, and
    house rule 3 forbids silencing that with a comment — so the untyped access is named once,
    where the reason for it can be written down, instead of three times where it cannot.
    """
    cache_clear = getattr(get_provider_settings, "cache_clear", None)
    if callable(cache_clear):
        cache_clear()


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("BASKFY_KITE_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("BASKFY_KITE_TOKEN_PATH", str(tmp_path / "kite.enc"))
    _reset_settings_cache()
    return tmp_path / "kite.enc"


class TestTheTokenReachesTheProvidersStore:
    def test_a_deposited_token_is_what_the_provider_will_read(
        self, configured: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole point: what goes in here is what `KiteProvider` picks up.

        Asserted by reading it back through `AccessTokenStore` — the same class the provider
        uses — rather than by trusting that a write happened.
        """
        assert kite_session_cli.main(["deposit", "--token", "live-token-xyz"]) == 0

        key = kite_session_cli.get_provider_settings().kite_token_encryption_key
        store = AccessTokenStore(configured, key)
        assert store.load().value == "live-token-xyz"

    def test_the_stored_file_is_not_readable_as_plaintext(self, configured: Path) -> None:
        """It is a live credential for an account that can trade."""
        kite_session_cli.main(["deposit", "--token", "live-token-xyz"])
        assert b"live-token-xyz" not in configured.read_bytes()

    def test_it_is_not_world_readable(self, configured: Path) -> None:
        kite_session_cli.main(["deposit", "--token", "live-token-xyz"])
        assert configured.stat().st_mode & 0o077 == 0

    def test_the_token_is_never_printed(
        self, configured: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """This prints to a terminal and usually into a scrollback somebody keeps."""
        kite_session_cli.main(["deposit", "--token", "live-token-xyz"])
        assert "live-token-xyz" not in capsys.readouterr().out


class TestItRefusesRatherThanFailLater:
    def test_no_encryption_key_is_refused_up_front(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Writing a broker credential unencrypted is not a degraded mode."""
        monkeypatch.setenv("BASKFY_KITE_TOKEN_ENCRYPTION_KEY", "")
        monkeypatch.setenv("BASKFY_KITE_TOKEN_PATH", str(tmp_path / "k.enc"))
        _reset_settings_cache()

        with pytest.raises(SystemExit):
            kite_session_cli.main(["deposit", "--token", "x"])

    def test_an_empty_token_is_refused(self, configured: Path) -> None:
        with pytest.raises(ValueError):
            kite_session_cli.main(["deposit", "--token", "   "])


class TestStatusAnswersTheQuestionThatMatters:
    def test_absent_is_a_failure_exit(self, configured: Path) -> None:
        """So a deploy check or a cron can branch on it."""
        assert kite_session_cli.main(["status"]) == 1

    def test_present_and_fresh_is_success(self, configured: Path) -> None:
        kite_session_cli.main(["deposit", "--token", "live-token-xyz"])
        assert kite_session_cli.main(["status"]) == 0

    def test_present_but_expired_is_a_failure_exit(
        self, configured: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A Kite token dies overnight with no refresh.

        "Present" and "usable" are different questions, and conflating them is how a pipeline
        discovers the problem at 6pm instead of at 9am.
        """
        key = kite_session_cli.get_provider_settings().kite_token_encryption_key
        store = AccessTokenStore(configured, key)
        store.save("stale", issued_at=dt.datetime.now(tz=dt.UTC) - dt.timedelta(days=3))
        assert kite_session_cli.main(["status"]) == 1
