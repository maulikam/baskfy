"""Encrypted storage for the Kite access token (Prompt 2 deliverable 2).

docs/09 §"Kite specifics": the token is *daily*, must be refreshed through the login flow, must be
stored encrypted, and its expiry is "the #1 pipeline failure" — so it has to alert loudly.
docs/11 §Security: "Kite access token encrypted at rest."

Two design points worth stating:

* **Expiry is computed, not trusted.** Kite tokens expire at the next trading day's pre-open
  rather than N hours after issue, so the store records when a token was issued and treats it as
  stale once the IST calendar day has rolled over. It errs towards declaring expiry early: a
  false "expired" costs one login, a false "valid" costs a whole night's pipeline run.
* **A stale token raises rather than returns.** Handing a known-dead token to the caller only
  moves the failure somewhere less legible.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from decile_providers.errors import AccessTokenExpired, CredentialsMissing, UnexpectedPayload

#: NSE trades in IST; the token's lifetime is bounded by the IST trading day.
IST = dt.timezone(dt.timedelta(hours=5, minutes=30), name="IST")

_PAYLOAD_VERSION = 1


@dataclass(frozen=True, slots=True)
class AccessToken:
    """A Kite access token and the moment it was issued."""

    value: str
    issued_at: dt.datetime

    def is_expired(self, *, now: dt.datetime | None = None) -> bool:
        """True once the IST calendar day of issue has passed.

        Kite invalidates access tokens at the start of the next trading day, not after a fixed
        interval, so the calendar date is the honest boundary. A token issued at 23:55 IST is
        treated as expired five minutes later — which is correct, and safer than the reverse.
        """
        moment = now or dt.datetime.now(tz=IST)
        return moment.astimezone(IST).date() > self.issued_at.astimezone(IST).date()


class AccessTokenStore:
    """Reads and writes the Fernet-encrypted token blob.

    The blob is versioned so a future migration (to a secret manager, say) can recognise what it
    is looking at instead of guessing.
    """

    def __init__(self, path: Path | str, encryption_key: str) -> None:
        self._path = Path(path)
        self._encryption_key = encryption_key

    @property
    def path(self) -> Path:
        return self._path

    def _fernet(self) -> Fernet:
        if not self._encryption_key:
            raise CredentialsMissing(
                "DECILE_KITE_TOKEN_ENCRYPTION_KEY is not set, so the access token cannot be "
                'decrypted. Generate one with `python -c "from cryptography.fernet import '
                'Fernet; print(Fernet.generate_key().decode())"` and store it as a secret.',
                provider="kite",
            )
        try:
            return Fernet(self._encryption_key.encode("utf-8"))
        except (ValueError, TypeError) as exc:
            raise CredentialsMissing(
                "DECILE_KITE_TOKEN_ENCRYPTION_KEY is not a valid Fernet key "
                "(32 url-safe base64-encoded bytes)",
                provider="kite",
            ) from exc

    def exists(self) -> bool:
        return self._path.is_file()

    def save(self, token: str, *, issued_at: dt.datetime | None = None) -> AccessToken:
        """Encrypt and persist a freshly obtained token."""
        if not token:
            raise ValueError("refusing to store an empty access token")
        record = AccessToken(value=token, issued_at=issued_at or dt.datetime.now(tz=IST))
        payload = json.dumps(
            {
                "version": _PAYLOAD_VERSION,
                "access_token": record.value,
                "issued_at": record.issued_at.isoformat(),
            }
        ).encode("utf-8")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_bytes(self._fernet().encrypt(payload))
        # The token is a live credential: keep it off other users' eyes on shared boxes.
        self._path.chmod(0o600)
        return record

    def load(self) -> AccessToken:
        """Decrypt the stored token. Does not check expiry — see :meth:`require_fresh`."""
        if not self.exists():
            raise CredentialsMissing(
                f"no Kite access token at {self._path}. Run the Kite login flow and store the "
                "result with `AccessTokenStore.save()`.",
                provider="kite",
            )
        try:
            decrypted = self._fernet().decrypt(self._path.read_bytes())
        except InvalidToken as exc:
            raise CredentialsMissing(
                f"the token blob at {self._path} could not be decrypted; the encryption key has "
                "probably been rotated. Re-run the login flow to write a fresh one.",
                provider="kite",
            ) from exc

        try:
            payload = json.loads(decrypted)
            return AccessToken(
                value=str(payload["access_token"]),
                issued_at=dt.datetime.fromisoformat(str(payload["issued_at"])),
            )
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            raise UnexpectedPayload(
                f"the token blob at {self._path} decrypted but is not a token record",
                provider="kite",
            ) from exc

    def require_fresh(self, *, now: dt.datetime | None = None) -> AccessToken:
        """Return a token known to be usable, or raise loudly.

        This is the only accessor the Kite adapter uses, so an expired token can never reach an
        API call by accident.
        """
        token = self.load()
        if token.is_expired(now=now):
            raise AccessTokenExpired(
                f"the Kite access token stored at {self._path} was issued on "
                f"{token.issued_at.astimezone(IST).date().isoformat()} and is no longer valid"
            )
        return token
