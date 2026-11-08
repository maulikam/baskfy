"""A bad link from an email answers in the route's own words, not Pydantic's.

`VerifyEmailIn.token` carried `min_length=8`, and that one constraint decided which of two very
different sentences a reader saw. A token that reached the handler got
*"That confirmation link is not valid or has already been used."* A token shorter than eight
characters never got there: Pydantic rejected it first, and the generic 400 for this API is
`invalid-screen-definition`, whose detail is *"The screen definition failed validation."*

So someone confirming an email address — who has never opened a screener — was told their screen
definition was invalid. Mail clients wrap and truncate long URLs, so that is a real reader's
experience, not only a tester's.

The floor bought nothing: these tokens are redeemed by comparing a SHA-256 digest, so a short
string fails to match on its own. The maximum stays, because it bounds work before hashing.

`VerifyEmailIn` and `ResetPasswordIn` — the two models this was written against — went with the
endpoints behind them when Google sign-in replaced the email/password funnel
(`docs/DECISIONS-MERGE.md` M46). `UnsubscribeIn` is the last token that arrives out of a link in
an email, and it is the one this now guards. The rule is kept parameterised over a list of one so
that the next such token is added rather than re-derived: nothing about the reasoning was specific
to email verification, and the wrong sentence would be just as wrong on an unsubscribe page.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

import pytest
from pydantic import ValidationError

from baskfy_api.schemas import UnsubscribeIn


class _CarriesAToken(Protocol):
    """The one field these models are gathered here to share."""

    token: str


#: Builds one request model from a token. Typed rather than suppressed: house rule 3.
#: `BaseModel` was too wide — it hid `.token`, the only attribute every test here reads.
Build = Callable[[str], _CarriesAToken]

#: Every request model whose token arrives out of a link in an email.
LINK_TOKEN_MODELS: list[tuple[str, Build]] = [
    ("UnsubscribeIn", lambda token: UnsubscribeIn(token=token)),
]
IDS = [name for name, _ in LINK_TOKEN_MODELS]


@pytest.mark.parametrize(("name", "build"), LINK_TOKEN_MODELS, ids=IDS)
class TestATruncatedTokenReachesTheRoute:
    def test_a_short_token_validates(self, name: str, build: Build) -> None:
        # It must *parse*. Whether it is valid is the route's question, and only the route can
        # answer it in language the reader understands.
        assert build("demo").token == "demo"

    def test_an_empty_token_validates(self, name: str, build: Build) -> None:
        # The most likely truncation of all, and still the route's to answer.
        assert build("").token == ""


@pytest.mark.parametrize(("name", "build"), LINK_TOKEN_MODELS, ids=IDS)
class TestTheCeilingStays:
    def test_an_absurd_token_is_refused_before_hashing(self, name: str, build: Build) -> None:
        # The maximum is not politeness — it caps the work an unauthenticated request can ask for
        # before anything computes a digest over it.
        with pytest.raises(ValidationError):
            build("x" * 4096)
