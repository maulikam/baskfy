"""AUDIT 2.6 — webhook URLs may not resolve to private / loopback / link-local."""

from __future__ import annotations

import pytest

from baskfy_api.webhooks import WebhookUrlNotPublic, assert_public_webhook_url


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/hook",
        "http://localhost/hook",
        "http://10.0.0.1/hook",
        "http://192.168.1.1/hook",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/hook",
    ],
)
def test_private_webhook_urls_are_refused(url: str) -> None:
    with pytest.raises(WebhookUrlNotPublic):
        assert_public_webhook_url(url)
