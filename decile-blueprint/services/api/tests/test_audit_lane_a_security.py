"""AUDIT 2.12 / 2.13 / 2.5 — rate-limit client IP, CORS CSRF header, empty allowlist."""

from __future__ import annotations

import inspect

import pytest
from starlette.requests import Request

from baskfy_api import app as app_module
from baskfy_api.ratelimit import _client_ip
from baskfy_api.settings import Settings


def _request(*, headers: dict[str, str] | None = None, client: str | None = "10.0.0.2") -> Request:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": (client, 12345) if client else None,
        "server": ("test", 80),
    }
    return Request(scope)


def test_rate_limit_prefers_x_forwarded_for_leftmost_hop() -> None:
    request = _request(headers={"x-forwarded-for": "203.0.113.9, 10.0.0.1"}, client="10.0.0.2")
    assert _client_ip(request) == "203.0.113.9"


def test_rate_limit_falls_back_to_asgi_client() -> None:
    assert _client_ip(_request()) == "10.0.0.2"


def test_production_refuses_empty_login_allowlist() -> None:
    settings = Settings(
        environment="production",
        jwt_secret="x" * 32,
        cookie_secure=True,
        email_transport="resend",
        resend_api_key="re_test",
        google_client_id="gid",
        razorpay_webhook_secret="whsec",
        razorpay_key_id="rzp",
        razorpay_key_secret="secret",
        supplier_gstin="27AAAAA0000A1Z5",
        supplier_state="Maharashtra",
        login_allowlist="",
    )
    with pytest.raises(RuntimeError, match="BASKFY_LOGIN_ALLOWLIST"):
        settings.require_configured()


def test_cors_allow_headers_include_csrf_token() -> None:
    source = inspect.getsource(app_module)
    assert '"X-CSRF-Token"' in source
