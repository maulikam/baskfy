"""The token bridge must move a token without ever revealing it.

`scripts/token_sync.py` exists so the laptop can reach Kite with the token the box minted, without
registering a second Redirect URL against a live trading app. Two properties matter enough to be
tested rather than reviewed: it reads both the format the box writes today and the one it will write
after its next deploy, and the token value never reaches stdout, stderr, or a log record.
"""

from __future__ import annotations

import importlib.util
import json
import logging
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "token_sync", Path(__file__).resolve().parent.parent / "scripts" / "token_sync.py"
)
assert _SPEC and _SPEC.loader
token_sync = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(token_sync)

SECRET = "sekrit-access-token-abc123"


def test_reads_the_plain_json_the_box_writes_today() -> None:
    blob = json.dumps({"access_token": SECRET, "date": "2026-08-22"}).encode()
    assert token_sync._extract(blob, "") == SECRET


def test_reads_the_encrypted_blob_the_box_will_write_after_its_next_deploy(tmp_path: Path) -> None:
    from cryptography.fernet import Fernet  # noqa: PLC0415  # noqa: PLC0415

    from baskfy_providers.tokens import AccessTokenStore  # noqa: PLC0415

    key = Fernet.generate_key().decode()
    store = AccessTokenStore(tmp_path / "t.enc", key)
    store.save(SECRET)
    assert token_sync._extract((tmp_path / "t.enc").read_bytes(), key) == SECRET


def test_encrypted_blob_without_a_key_fails_with_an_actionable_message(tmp_path: Path) -> None:
    from cryptography.fernet import Fernet  # noqa: PLC0415  # noqa: PLC0415

    from baskfy_providers.tokens import AccessTokenStore  # noqa: PLC0415

    AccessTokenStore(tmp_path / "t.enc", Fernet.generate_key().decode()).save(SECRET)
    with pytest.raises(SystemExit) as exc:
        token_sync._extract((tmp_path / "t.enc").read_bytes(), "")
    assert "--remote-key" in str(exc.value)


def test_json_without_an_access_token_is_refused_not_silently_empty() -> None:
    with pytest.raises(SystemExit):
        token_sync._extract(json.dumps({"date": "2026-08-22"}).encode(), "")


def test_the_token_never_appears_in_stdout_stderr_or_a_log_record(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The point of the bridge. main() end to end, everything faked but the printing."""
    from cryptography.fernet import Fernet  # noqa: PLC0415

    monkeypatch.setenv("KITE_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(
        token_sync, "_fetch", lambda *a, **k: json.dumps({"access_token": SECRET}).encode()
    )
    monkeypatch.setattr(token_sync, "_verify", lambda api_key, token: "individual")
    monkeypatch.setattr("sys.argv", ["token_sync.py", "--target", "desk@example"])

    from app import config as C  # noqa: PLC0415

    monkeypatch.setattr(C, "TOKEN_FILE", str(tmp_path / ".kite_token"))

    with caplog.at_level(logging.DEBUG):
        assert token_sync.main() == 0

    out = capsys.readouterr()
    assert SECRET not in out.out, "the token reached stdout"
    assert SECRET not in out.err, "the token reached stderr"
    assert SECRET not in caplog.text, "the token reached a log record"
    assert "individual" in out.out, "it should say the call succeeded"

    # ...and what it did write is encrypted, not the token in the clear.
    written = (tmp_path / ".kite_token").read_bytes()
    assert SECRET.encode() not in written


def test_verification_is_one_cheap_call_not_a_data_pull() -> None:
    """profile() and nothing heavier. A historical_data() probe would cost a quota call and, on the
    free data tier, 403 for a reason that has nothing to do with the token being valid."""
    src = (Path(token_sync.__file__)).read_text()
    body = src[src.index("def _verify") : src.index("def _fingerprint")]
    assert "kc.profile()" in body
    assert "historical_data" not in body
