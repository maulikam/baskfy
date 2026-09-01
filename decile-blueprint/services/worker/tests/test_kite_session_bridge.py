"""The bridge around one redirect URL — `baskfy_worker.kite_session_cli`.

A Kite Connect app has exactly one registered redirect URL, and the RENIL app's points at the
momentum desk, which places live orders and cannot lose its login. Baskfy therefore can never
*start* a Kite login. It does not need to: `KiteProvider` performs no OAuth, it reads an access
token out of `AccessTokenStore`. The desk obtains that token every morning through the redirect it
owns; this carries it to where Baskfy looks.
"""

from __future__ import annotations

import datetime as dt
import subprocess
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet

from baskfy_providers.settings import get_provider_settings
from baskfy_providers.tokens import AccessTokenStore
from baskfy_worker import kite_session_cli
from baskfy_worker.tasks import celery_tasks


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

        key = get_provider_settings().kite_token_encryption_key
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
        key = get_provider_settings().kite_token_encryption_key
        store = AccessTokenStore(configured, key)
        store.save("stale", issued_at=dt.datetime.now(tz=dt.UTC) - dt.timedelta(days=3))
        assert kite_session_cli.main(["status"]) == 1


# ---------------------------------------------------------------------------------------------
# `pull` — the bridge without a human in it (M58)
# ---------------------------------------------------------------------------------------------

#: A plausible Kite access token. Kite's are 32 characters; the code accepts a range because
#: Zerodha documents no format, and a test that pinned 32 would assert today's behaviour rather
#: than the spec (house rule 2).
DESK_TOKEN = "AbCd1234EfGh5678IjKl9012MnOp3456"


class _FakeCompleted:
    """Stands in for `subprocess.CompletedProcess` without importing its generic parameter."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def pullable(configured: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A deployment that is allowed to pull: encryption key, api key, desk target, key path.

    Kite is stubbed to answer "live session" so that tests about *fetching* a token are not also
    tests about verifying one. The verification tests override this stub with their own.
    """
    monkeypatch.setenv("BASKFY_KITE_API_KEY", "apikey0123456789")
    monkeypatch.setenv("BASKFY_KITE_DESK_SSH_TARGET", "desk@desk.example")
    monkeypatch.setenv("BASKFY_KITE_DESK_SSH_KEY_PATH", str(tmp_path / "id_ed25519"))
    _reset_settings_cache()

    def live_session(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "success", "data": {"user_id": "YP8452"}},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx, "get", live_session)
    return configured


def _record_run(
    monkeypatch: pytest.MonkeyPatch, result: _FakeCompleted, seen: list[list[str]]
) -> None:
    def fake_run(command: list[str], **kwargs: object) -> _FakeCompleted:
        seen.append(command)
        # A shell would re-interpret whatever the desk sends back. Assert we never ask for one.
        assert kwargs.get("shell") in (None, False)
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)


class TestPullFetchesTheSessionWithoutAHuman:
    def test_what_the_desk_returns_is_what_the_provider_will_read(
        self, pullable: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The point of the whole mechanism, asserted end to end through the real store."""
        seen: list[list[str]] = []
        _record_run(monkeypatch, _FakeCompleted(0, stdout=DESK_TOKEN + "\n"), seen)

        assert kite_session_cli.pull() == 0

        settings = get_provider_settings()
        store = AccessTokenStore(settings.kite_token_path, settings.kite_token_encryption_key)
        assert store.load().value == DESK_TOKEN

    def test_it_never_waits_for_a_password_it_cannot_be_given(
        self, pullable: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A nightly job has no terminal. Prompting is not a slow failure, it is a hang."""
        seen: list[list[str]] = []
        _record_run(monkeypatch, _FakeCompleted(0, stdout=DESK_TOKEN), seen)
        kite_session_cli.pull()

        flat = " ".join(seen[0])
        assert "BatchMode=yes" in flat
        assert "PasswordAuthentication=no" in flat

    def test_it_verifies_the_desk_rather_than_trusting_whoever_answers(
        self, pullable: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`accept-new` delegates a security decision to a job with nobody watching.

        The desk hands out a credential that can trade. Pinning its host key is the difference
        between "the desk told us the token" and "something at that address did".
        """
        seen: list[list[str]] = []
        _record_run(monkeypatch, _FakeCompleted(0, stdout=DESK_TOKEN), seen)
        kite_session_cli.pull()

        flat = " ".join(seen[0])
        assert "StrictHostKeyChecking=yes" in flat
        assert "accept-new" not in flat
        assert "StrictHostKeyChecking=no" not in flat
        assert "UserKnownHostsFile=" in flat

    def test_a_deployment_without_a_desk_is_told_what_to_do_instead(
        self, configured: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("BASKFY_KITE_DESK_SSH_TARGET", raising=False)
        _reset_settings_cache()
        with pytest.raises(SystemExit) as caught:
            kite_session_cli.fetch_desk_token()
        assert "deposit" in str(caught.value)


class TestTheTokenIsNeverExposed:
    def test_it_is_not_passed_on_a_command_line(
        self, pullable: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`ps` shows every argument of every running process to every user on the box.

        The token must therefore arrive on stdout, never as an argv element.
        """
        seen: list[list[str]] = []
        _record_run(monkeypatch, _FakeCompleted(0, stdout=DESK_TOKEN), seen)
        kite_session_cli.pull()

        assert seen, "ssh was never invoked"
        assert all(DESK_TOKEN not in argument for argument in seen[0])

    def test_success_prints_the_length_and_not_the_secret(
        self, pullable: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _record_run(monkeypatch, _FakeCompleted(0, stdout=DESK_TOKEN), [])
        kite_session_cli.pull()
        captured = capsys.readouterr()
        assert DESK_TOKEN not in captured.out + captured.err
        assert str(len(DESK_TOKEN)) in captured.out

    def test_a_failure_message_does_not_quote_stdout_back(
        self, pullable: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The leak this guards.

        A non-zero exit with a token already on stdout is a real shape — the forced command can
        emit the token and *then* something can fail. Echoing stdout into the error would put a
        live broker credential into the log of every failed run.
        """
        _record_run(
            monkeypatch, _FakeCompleted(255, stdout=DESK_TOKEN, stderr="Connection closed"), []
        )
        with pytest.raises(SystemExit) as caught:
            kite_session_cli.fetch_desk_token()
        assert DESK_TOKEN not in str(caught.value)
        assert "Connection closed" in str(caught.value)


class TestItRefusesAnythingThatIsNotAToken:
    @pytest.mark.parametrize(
        "stdout",
        [
            "",
            "Permission denied (publickey).",
            "Welcome to Ubuntu 24.04 LTS\n" + DESK_TOKEN,
            "Traceback (most recent call last):\n  File x",
            "short",
        ],
        ids=["empty", "ssh-error", "motd-prefix", "traceback", "too-short"],
    )
    def test_it_stores_nothing_when_the_desk_answers_with_prose(
        self, pullable: Path, monkeypatch: pytest.MonkeyPatch, stdout: str
    ) -> None:
        """Storing prose succeeds here and fails hours later as an unexplained provider error.

        `motd-prefix` is the case worth naming: a banner on the desk turns a *valid* token into a
        multi-line answer, and a lenient parser would happily store the banner.
        """
        _record_run(monkeypatch, _FakeCompleted(0, stdout=stdout), [])
        with pytest.raises(SystemExit):
            kite_session_cli.fetch_desk_token()

        settings = get_provider_settings()
        assert not AccessTokenStore(
            settings.kite_token_path, settings.kite_token_encryption_key
        ).exists()

    def test_an_unreachable_desk_names_the_timeout(
        self, pullable: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(command: list[str], **kwargs: object) -> _FakeCompleted:
            raise subprocess.TimeoutExpired(cmd="ssh", timeout=20.0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(SystemExit) as caught:
            kite_session_cli.fetch_desk_token()
        assert "20" in str(caught.value)

    def test_an_image_without_ssh_says_so_plainly(
        self, pullable: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The failure mode of forgetting `openssh-client` in the Dockerfile."""

        def fake_run(command: list[str], **kwargs: object) -> _FakeCompleted:
            raise FileNotFoundError(2, "No such file or directory: 'ssh'")

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(SystemExit) as caught:
            kite_session_cli.fetch_desk_token()
        assert "openssh-client" in str(caught.value)


class TestAFailedPullDoesNotStopTheNight:
    def test_refresh_quietly_reports_and_returns(
        self, pullable: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """House rule: the day's bars come from the bhavcopy, which needs no Kite session.

        So an unreachable desk must degrade the night, not end it.
        """
        _record_run(monkeypatch, _FakeCompleted(255, stderr="Network is unreachable"), [])
        assert kite_session_cli.refresh_quietly() is False
        assert "not refreshed" in capsys.readouterr().err

    def test_refresh_quietly_returns_true_when_it_worked(
        self, pullable: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _record_run(monkeypatch, _FakeCompleted(0, stdout=DESK_TOKEN), [])
        assert kite_session_cli.refresh_quietly() is True


class TestTheNightRefreshesItsOwnSession:
    """The reason the bridge exists: nobody should have to remember to run it.

    `nightly_pipeline` is called directly rather than through Celery — the schedule is Celery's
    concern, and what is under test is what the task does when it runs.
    """

    def test_it_borrows_the_desks_session_before_it_fetches_anything(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Order is the assertion. Refreshing *after* the fetch would be a no-op for the night.

        The token stored yesterday expired overnight, so a fetch that runs first is a fetch with
        no session at all — the exact failure that left the site on 18 Aug 2026 for ten days.
        """

        class _Stop(Exception):
            """Ends the task at a known point, so nothing downstream needs a database."""

        calls: list[str] = []

        def fake_refresh() -> bool:
            calls.append("refresh")
            return True

        def fake_deps() -> object:
            calls.append("deps")
            raise _Stop

        monkeypatch.setattr(kite_session_cli, "refresh_quietly", fake_refresh)
        monkeypatch.setattr(celery_tasks, "build_pipeline_dependencies", fake_deps)

        with pytest.raises(_Stop):
            celery_tasks.nightly_pipeline("2026-08-28")

        assert calls == ["refresh", "deps"]

    def test_an_unreachable_desk_does_not_cancel_the_night(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The bhavcopy still has the day's bars. Failing here would throw them away."""

        class _Stop(Exception):
            pass

        reached: list[str] = []

        def fake_deps() -> object:
            reached.append("deps")
            raise _Stop

        monkeypatch.setattr(kite_session_cli, "refresh_quietly", lambda: False)
        monkeypatch.setattr(celery_tasks, "build_pipeline_dependencies", fake_deps)

        with pytest.raises(_Stop):
            celery_tasks.nightly_pipeline("2026-08-28")

        assert reached == ["deps"], "a failed pull must not stop the pipeline"


class TestAPulledSessionIsProvenBeforeItIsTrusted:
    """A token that reads back perfectly can still be dead.

    The desk's file holds yesterday's string until someone logs in again, and nothing about
    fetching or decrypting it notices. Only Kite can answer whether it is a session.
    """

    def _respond(
        self, monkeypatch: pytest.MonkeyPatch, status: int, payload: object
    ) -> list[dict[str, str]]:
        seen: list[dict[str, str]] = []

        def fake_get(url: str, **kwargs: object) -> httpx.Response:
            headers = kwargs.get("headers")
            if isinstance(headers, dict):
                seen.append({str(k): str(v) for k, v in headers.items()})
            return httpx.Response(status, json=payload, request=httpx.Request("GET", url))

        monkeypatch.setattr(httpx, "get", fake_get)
        return seen

    def test_a_live_token_is_stored_and_its_owner_named(
        self, pullable: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("BASKFY_KITE_API_KEY", "apikey0123456789")
        _reset_settings_cache()
        _record_run(monkeypatch, _FakeCompleted(0, stdout=DESK_TOKEN), [])
        self._respond(monkeypatch, 200, {"status": "success", "data": {"user_id": "YP8452"}})

        assert kite_session_cli.pull() == 0
        assert "YP8452" in capsys.readouterr().out

    def test_a_403_names_both_causes_because_only_one_of_them_is_the_operators_fault(
        self, pullable: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Kite answers 403 for a dead token and for an un-whitelisted host alike.

        The message therefore says both. Guessing one would send the reader to the wrong box.
        """
        monkeypatch.setenv("BASKFY_KITE_API_KEY", "apikey0123456789")
        _reset_settings_cache()
        _record_run(monkeypatch, _FakeCompleted(0, stdout=DESK_TOKEN), [])
        self._respond(monkeypatch, 403, {"status": "error"})

        with pytest.raises(SystemExit) as caught:
            kite_session_cli.pull()
        message = str(caught.value)
        assert "logged in" in message
        assert "whitelisted" in message

    def test_a_token_that_fails_verification_is_not_stored(
        self, pullable: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The point of verifying *before* storing.

        A stored dead token and no token both leave the pipeline sessionless — but the stored one
        also hides the reason, and the reason is the only part anyone can act on.
        """
        monkeypatch.setenv("BASKFY_KITE_API_KEY", "apikey0123456789")
        _reset_settings_cache()
        _record_run(monkeypatch, _FakeCompleted(0, stdout=DESK_TOKEN), [])
        self._respond(monkeypatch, 403, {"status": "error"})

        with pytest.raises(SystemExit):
            kite_session_cli.pull()

        settings = get_provider_settings()
        assert not AccessTokenStore(
            settings.kite_token_path, settings.kite_token_encryption_key
        ).exists()

    def test_the_token_travels_in_a_header_and_not_a_query_string(
        self, pullable: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Query strings are logged by proxies and leak through referrers. Headers are not."""
        monkeypatch.setenv("BASKFY_KITE_API_KEY", "apikey0123456789")
        _reset_settings_cache()
        _record_run(monkeypatch, _FakeCompleted(0, stdout=DESK_TOKEN), [])
        seen = self._respond(monkeypatch, 200, {"status": "success", "data": {"user_id": "YP8452"}})
        kite_session_cli.pull()

        assert seen, "Kite was never called"
        assert DESK_TOKEN in seen[0]["Authorization"]

    def test_a_profile_without_a_user_id_is_not_treated_as_proof(
        self, pullable: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 200 is not the assertion; a session belonging to somebody is."""
        monkeypatch.setenv("BASKFY_KITE_API_KEY", "apikey0123456789")
        _reset_settings_cache()
        _record_run(monkeypatch, _FakeCompleted(0, stdout=DESK_TOKEN), [])
        self._respond(monkeypatch, 200, {"status": "success", "data": {}})

        with pytest.raises(SystemExit) as caught:
            kite_session_cli.pull()
        assert "user_id" in str(caught.value)

    def test_no_api_key_is_named_as_the_missing_piece(
        self, pullable: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`KiteProvider` needs api_key + token. A token on its own calls nothing."""
        monkeypatch.setenv("BASKFY_KITE_API_KEY", "")
        _reset_settings_cache()
        with pytest.raises(SystemExit) as caught:
            kite_session_cli.verify_session(DESK_TOKEN)
        assert "BASKFY_KITE_API_KEY" in str(caught.value)

    def test_verification_can_be_skipped_for_a_host_that_cannot_reach_kite(
        self, pullable: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The escape hatch, and proof it is a real one: Kite is never called."""
        called: list[str] = []

        def fake_get(url: str, **kwargs: object) -> httpx.Response:
            called.append(url)
            raise AssertionError("Kite must not be called when verification is off")

        monkeypatch.setattr(httpx, "get", fake_get)
        _record_run(monkeypatch, _FakeCompleted(0, stdout=DESK_TOKEN), [])

        assert kite_session_cli.pull(verify=False) == 0
        assert called == []
