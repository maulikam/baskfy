"""Double-submit CSRF for the cookie-authenticated mutations — docs/11 §Security (Prompt 12).

    "CSRF protection on all cookie-authenticated mutations."

Only two endpoints are cookie-authenticated: ``POST /auth/refresh`` and ``POST /auth/logout``.
Everything else on the API is authorised by an ``Authorization: Bearer`` header, which a browser
never attaches automatically and which therefore cannot be forged cross-site in the first place.

The construction
----------------
On login the API sets two cookies:

* ``decile_refresh`` — httpOnly, ``SameSite=Lax``, Secure. The credential. JavaScript cannot read
  it, which is what makes it worth stealing and what makes it un-stealable by XSS.
* ``decile_csrf`` — **not** httpOnly, same lifetime. A random value with no authority of its own.

A refresh must present the second one in the ``X-CSRF-Token`` header. A cross-site attacker can
cause the browser to *send* the cookies but cannot *read* them to populate the header, because the
same-origin policy stops them reading a response from our origin. So the header proves the request
came from a page that could read our cookies.

``SameSite=Lax`` already stops a cross-site `POST` from carrying the cookie at all, so this is the
second lock rather than the only one — which is the point: Lax has known gaps (a same-site
subdomain, a browser that has not shipped the default) and a credential with a 30-day life should
not depend on one mechanism.

Both are compared in constant time. The value is never logged.
"""

from __future__ import annotations

import hmac
import secrets
from typing import Final

from fastapi import Request, Response

from decile_api.problems import Problem, ProblemType
from decile_api.settings import Settings

#: The httpOnly credential. Named for the product so it cannot collide with the web app's own.
REFRESH_COOKIE: Final = "decile_refresh"

#: The readable half of the double submit.
CSRF_COOKIE: Final = "decile_csrf"

CSRF_HEADER: Final = "X-CSRF-Token"

#: 128 bits is plenty for a value that only has to be unguessable for the length of a session.
CSRF_TOKEN_BYTES: Final = 16

#: docs/07 §"Account & billing" scopes the refresh cookie to the auth endpoints. A cookie sent on
#: every request to every path is a credential exposed far more often than it is needed.
REFRESH_COOKIE_PATH: Final = "/api/v1/auth"


def new_csrf_token() -> str:
    return secrets.token_urlsafe(CSRF_TOKEN_BYTES)


def set_auth_cookies(
    response: Response, *, refresh_token: str, csrf_token: str, ttl_seconds: int, settings: Settings
) -> None:
    """docs/11: "rotating refresh in an httpOnly, `SameSite=Lax`, Secure cookie"."""
    domain = settings.cookie_domain or None
    response.set_cookie(
        REFRESH_COOKIE,
        refresh_token,
        max_age=ttl_seconds,
        path=REFRESH_COOKIE_PATH,
        domain=domain,
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf_token,
        max_age=ttl_seconds,
        path="/",
        domain=domain,
        secure=settings.cookie_secure,
        # Readable on purpose: the page has to copy it into the header. It authorises nothing on
        # its own — presenting it without the httpOnly cookie refreshes nothing.
        httponly=False,
        samesite="lax",
    )


def clear_auth_cookies(response: Response, settings: Settings) -> None:
    domain = settings.cookie_domain or None
    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_COOKIE_PATH, domain=domain)
    response.delete_cookie(CSRF_COOKIE, path="/", domain=domain)


def require_csrf(request: Request) -> None:
    """Raise a 401 unless the header matches the readable cookie.

    A 401 rather than a 403: from the caller's side "you did not prove this request came from our
    page" and "you are not signed in" are the same remedy — sign in again.
    """
    cookie = request.cookies.get(CSRF_COOKIE)
    header = request.headers.get(CSRF_HEADER)
    if not cookie or not header or not hmac.compare_digest(cookie, header):
        raise Problem(
            ProblemType.UNAUTHENTICATED,
            "This request is missing its CSRF token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
