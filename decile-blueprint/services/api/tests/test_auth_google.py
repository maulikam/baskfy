"""The front door, tested with real crypto — `baskfy_api.auth_google`.

Google sign-in is the only way into the product (`docs/DECISIONS-MERGE.md` M46), so this module
decides who gets in. Everywhere else in the suite a stub stands in for it, because reproducing an
RSA keypair per test would be testing this file forty times over. Here it is the subject, and
nothing is stubbed except the JWKS fetch — the tokens are really signed and really verified.

WHAT EACH TEST IS DEFENDING
---------------------------
The audience check is the one people skip, and skipping it is the difference between "a token
signed by Google" and "a token signed by Google *for us*". A valid ID token minted for somebody
else's OAuth client is a perfectly good token; accepting one means anybody who can get a user to
sign into their app can replay it here and become that user. `test_a_token_for_another_client_is_
refused` is that.

The algorithm pin is the second. Google's JWKS is public, so an attacker holds the modulus. Left
unpinned, PyJWT would accept an HS256 token signed *with* that public key as though it were a
signature — the classic confusion attack.

`email_verified` is the third, and the least obvious: Google will assert an address the account
has not proven control of. We resolve accounts by verified address on first sign-in, so believing
an unverified one hands over somebody else's account.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
from typing import Final

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from baskfy_api.auth_google import (
    GOOGLE_JWKS_URI,
    GoogleVerificationError,
    GoogleVerifier,
)
from baskfy_api.settings import Settings

CLIENT_ID: Final = "1234567890-baskfy.apps.googleusercontent.com"
OTHER_CLIENT: Final = "9999999999-somebody-else.apps.googleusercontent.com"
SUBJECT: Final = "104729384756102938475"
EMAIL: Final = "person@example.com"
KEY_ID: Final = "test-key-1"


@pytest.fixture(scope="module")
def signing_key() -> rsa.RSAPrivateKey:
    """One keypair for the module — generating 2048-bit RSA per test is seconds of nothing."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


# One keyword-only knob per thing that can be wrong — which is what "one thing wrong at a
# time" requires. Fewer parameters would mean fewer independently-testable failure modes.
def build_token(  # noqa: PLR0913
    key: rsa.RSAPrivateKey,
    *,
    audience: str = CLIENT_ID,
    issuer: str = "https://accounts.google.com",
    email: str | None = EMAIL,
    email_verified: object = True,
    subject: str | None = SUBJECT,
    name: str | None = "Asha Rao",
    expires_in: int = 3600,
    algorithm: str = "RS256",
    key_for_signing: rsa.RSAPrivateKey | None = None,
) -> str:
    """A token shaped exactly like Google's, with one thing wrong at a time."""
    now = dt.datetime.now(tz=dt.UTC)
    claims: dict[str, object] = {
        "iss": issuer,
        "aud": audience,
        "iat": int(now.timestamp()),
        "exp": int((now + dt.timedelta(seconds=expires_in)).timestamp()),
    }
    if subject is not None:
        claims["sub"] = subject
    if email is not None:
        claims["email"] = email
    if email_verified is not None:
        claims["email_verified"] = email_verified
    if name is not None:
        claims["name"] = name
    secret = key_for_signing if key_for_signing is not None else key
    return jwt.encode(claims, secret, algorithm=algorithm, headers={"kid": KEY_ID})


def _hs256_by_hand(*, secret: str, header: dict[str, object], claims: dict[str, object]) -> str:
    """A JWT built segment by segment, so an HS256 token can carry a PEM public key as its secret.

    Deliberately not `jwt.encode`: PyJWT blocks exactly this, which is correct of PyJWT and
    irrelevant to an attacker writing three base64 segments and an HMAC.
    """

    def segment(payload: dict[str, object]) -> bytes:
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=")

    signing_input = segment(header) + b"." + segment(claims)
    signature = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    return (signing_input + b"." + base64.urlsafe_b64encode(signature).rstrip(b"=")).decode()


@pytest.fixture
def verifier(signing_key: rsa.RSAPrivateKey, monkeypatch: pytest.MonkeyPatch) -> GoogleVerifier:
    """A real verifier whose JWKS lookup returns our test key instead of fetching Google's.

    Only the *fetch* is replaced. Signature verification, the audience check, the expiry and every
    claim assertion run exactly as they do in production — which is the point: a test that stubbed
    `jwt.decode` would assert nothing about whether this module calls it correctly.
    """
    built = GoogleVerifier(Settings(google_client_id=CLIENT_ID))

    class Signing:
        key = signing_key.public_key()

    def fake_lookup(self: object, token: str) -> Signing:
        del self, token
        return Signing()

    monkeypatch.setattr(jwt.PyJWKClient, "get_signing_key_from_jwt", fake_lookup)
    return built


class TestATokenWeShouldBelieve:
    async def test_it_yields_the_identity(
        self, verifier: GoogleVerifier, signing_key: rsa.RSAPrivateKey
    ) -> None:
        identity = await verifier.verify(build_token(signing_key))
        assert identity.subject == SUBJECT
        assert identity.email == EMAIL
        assert identity.name == "Asha Rao"

    async def test_the_other_issuer_spelling_is_accepted(
        self, verifier: GoogleVerifier, signing_key: rsa.RSAPrivateKey
    ) -> None:
        """Google issues both. Accepting only one rejects live users."""
        identity = await verifier.verify(build_token(signing_key, issuer="accounts.google.com"))
        assert identity.subject == SUBJECT

    async def test_email_verified_as_the_string_true_is_accepted(
        self, verifier: GoogleVerifier, signing_key: rsa.RSAPrivateKey
    ) -> None:
        """Some Workspace tokens have carried it as a string. Both spellings, nothing else."""
        identity = await verifier.verify(build_token(signing_key, email_verified="true"))
        assert identity.email == EMAIL

    async def test_a_missing_name_is_not_an_error(
        self, verifier: GoogleVerifier, signing_key: rsa.RSAPrivateKey
    ) -> None:
        """`name` is a display convenience, not an identity claim; the account works without it."""
        identity = await verifier.verify(build_token(signing_key, name=None))
        assert identity.name is None


class TestATokenWeMustRefuse:
    """Every one of these is a way in if the check is missing."""

    async def test_a_token_for_another_client_is_refused(
        self, verifier: GoogleVerifier, signing_key: rsa.RSAPrivateKey
    ) -> None:
        """The audience check. A correctly signed Google token minted for a different app.

        Without this, anyone who can get a user through *their* Google sign-in holds a token that
        signs that user into Baskfy.
        """
        with pytest.raises(GoogleVerificationError):
            await verifier.verify(build_token(signing_key, audience=OTHER_CLIENT))

    async def test_a_token_signed_by_somebody_else_is_refused(
        self, verifier: GoogleVerifier
    ) -> None:
        impostor = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with pytest.raises(GoogleVerificationError):
            await verifier.verify(build_token(impostor, key_for_signing=impostor))

    async def test_an_hs256_token_signed_with_the_public_key_is_refused(
        self, verifier: GoogleVerifier, signing_key: rsa.RSAPrivateKey
    ) -> None:
        """The algorithm-confusion attack the ``algorithms=["RS256"]`` pin exists to stop.

        Google's JWKS is public, so an attacker has the modulus. If the verifier accepted HS256,
        that public value becomes a shared secret and anybody can mint a valid token.

        The token is assembled by hand because PyJWT refuses to *encode* HS256 with a PEM public
        key — a guard on the signing side that an attacker simply does not use. Building the three
        segments directly is what the attack actually looks like, and it is the only way to put
        this verifier in front of one.
        """
        public_pem = (
            signing_key.public_key()
            .public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode()
        )
        now = dt.datetime.now(tz=dt.UTC)
        forged = _hs256_by_hand(
            secret=public_pem,
            header={"alg": "HS256", "typ": "JWT", "kid": KEY_ID},
            claims={
                "iss": "https://accounts.google.com",
                "aud": CLIENT_ID,
                "sub": SUBJECT,
                "email": EMAIL,
                "email_verified": True,
                "iat": int(now.timestamp()),
                "exp": int((now + dt.timedelta(hours=1)).timestamp()),
            },
        )
        with pytest.raises(GoogleVerificationError):
            await verifier.verify(forged)

    async def test_an_expired_token_is_refused(
        self, verifier: GoogleVerifier, signing_key: rsa.RSAPrivateKey
    ) -> None:
        with pytest.raises(GoogleVerificationError):
            await verifier.verify(build_token(signing_key, expires_in=-3600))

    async def test_an_unverified_address_is_refused(
        self, verifier: GoogleVerifier, signing_key: rsa.RSAPrivateKey
    ) -> None:
        with pytest.raises(GoogleVerificationError):
            await verifier.verify(build_token(signing_key, email_verified=False))

    async def test_a_missing_email_verified_claim_is_refused(
        self, verifier: GoogleVerifier, signing_key: rsa.RSAPrivateKey
    ) -> None:
        """An absent claim is not consent. Defaulting it to true would be the whole bug."""
        with pytest.raises(GoogleVerificationError):
            await verifier.verify(build_token(signing_key, email_verified=None))

    async def test_a_token_from_a_different_issuer_is_refused(
        self, verifier: GoogleVerifier, signing_key: rsa.RSAPrivateKey
    ) -> None:
        with pytest.raises(GoogleVerificationError):
            await verifier.verify(build_token(signing_key, issuer="https://evil.example"))

    async def test_a_token_with_no_subject_is_refused(
        self, verifier: GoogleVerifier, signing_key: rsa.RSAPrivateKey
    ) -> None:
        with pytest.raises(GoogleVerificationError):
            await verifier.verify(build_token(signing_key, subject=None))

    async def test_a_token_with_no_email_is_refused(
        self, verifier: GoogleVerifier, signing_key: rsa.RSAPrivateKey
    ) -> None:
        with pytest.raises(GoogleVerificationError):
            await verifier.verify(build_token(signing_key, email=None))

    async def test_garbage_is_refused_without_raising_anything_else(
        self, verifier: GoogleVerifier
    ) -> None:
        """A malformed token must be one `GoogleVerificationError`, not a `PyJWTError` escaping."""
        with pytest.raises(GoogleVerificationError):
            await verifier.verify("not-a-jwt")


class TestConfiguration:
    async def test_an_unconfigured_verifier_refuses_everything(
        self, signing_key: rsa.RSAPrivateKey
    ) -> None:
        """No client id means Google sign-in is *off*, never "accept any audience".

        This is the failure mode that matters most: an empty setting must close the door, not
        open it wide. `Settings.require_configured` refuses this state in production outright.
        """
        unconfigured = GoogleVerifier(Settings(google_client_id=""))
        assert unconfigured.configured is False
        with pytest.raises(GoogleVerificationError):
            await unconfigured.verify(build_token(signing_key))

    async def test_an_empty_token_is_refused_before_any_network_call(
        self, verifier: GoogleVerifier
    ) -> None:
        with pytest.raises(GoogleVerificationError):
            await verifier.verify("")

    def test_it_points_at_googles_published_jwks(self) -> None:
        """Pinned as a constant so a typo cannot silently redirect trust to another host."""
        assert GOOGLE_JWKS_URI == "https://www.googleapis.com/oauth2/v3/certs"
