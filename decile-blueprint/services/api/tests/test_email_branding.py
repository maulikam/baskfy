"""Every message carries the mark, and survives a client that refuses to load it.

The masthead is the first thing a reader sees from us and it goes out on the verification email —
the one message every account receives before it trusts anything. Two failure modes matter, and
they pull in opposite directions: no branding at all, and branding that renders as a broken-image
placeholder because Gmail blocks remote images by default.

So these assert the belt *and* the braces: the image is present, and the name is legible without
it.
"""

from __future__ import annotations

import re
from collections.abc import Callable

import pytest

from baskfy_api.email import templates as mail
from baskfy_api.email.templates import Message

#: A message factory. Typed rather than left to `noqa: ANN001`, because an untyped parameter in a
#: parametrized test is exactly the kind of hole house rule 3 exists to close.
Build = Callable[[], Message]

TEMPLATES: list[tuple[str, Build]] = [
    ("verify_email", lambda: mail.verify_email("a@b.com", "https://x.test/verify?token=t", 24)),
    ("otp", lambda: mail.otp("a@b.com", "123456", 10)),
    ("password_reset", lambda: mail.password_reset("a@b.com", "https://x.test/reset?t=1", 30)),
    ("lockout", lambda: mail.lockout("a@b.com", 15, 5)),
]


@pytest.mark.parametrize(("name", "build"), TEMPLATES, ids=[n for n, _ in TEMPLATES])
class TestEveryMessageIsBranded:
    def test_carries_the_mark(self, name: str, build: Build) -> None:
        assert mail.LOGO_URL in build().html, f"{name} has no logo"

    def test_the_name_survives_a_blocked_image(self, name: str, build: Build) -> None:
        # Gmail, Outlook and Apple Mail all block remote images until the reader allows them. Strip
        # every <img> and the brand must still be readable, or the first message an account ever
        # receives is an empty box.
        without_images = re.sub(r"<img[^>]*>", "", build().html)
        assert "Baskfy" in without_images, f"{name} names itself only inside an image"

    def test_the_mark_is_decorative_to_a_screen_reader(self, name: str, build: Build) -> None:
        # The word sits beside it in real text. A non-empty alt would say "Baskfy" twice.
        for tag in re.findall(r"<img[^>]*>", build().html):
            assert 'alt=""' in tag, f"{name}: the mark should be alt=\"\", got {tag}"


class TestTheMarkIsFetchable:
    def test_it_is_a_png_not_the_site_svg(self) -> None:
        # Gmail strips <img> pointing at SVG outright, and the site's own mark is an SVG. Using it
        # here would be correct-looking and invisible in the client most readers use.
        assert mail.LOGO_URL.endswith(".png")

    def test_it_is_absolute(self) -> None:
        # There is no page context in an inbox; a relative path resolves against nothing.
        assert mail.LOGO_URL.startswith("https://")

    def test_it_lives_under_the_path_the_gate_leaves_open(self) -> None:
        # `infra/docker/Caddyfile` exempts `/brand/*` from basic_auth precisely so a mail client,
        # which fetches with no session, can load this. A logo anywhere else 401s and renders
        # broken for every reader.
        assert "/brand/" in mail.LOGO_URL


class TestPlainTextIsUnaffected:
    def test_no_markup_leaks_into_the_text_part(self) -> None:
        # Prompt 12 §3: "plain-text alternatives included". The masthead is HTML-only; a `<table>`
        # appearing in the text part would be visible characters in a plain-text client.
        text = mail.verify_email("a@b.com", "https://x.test/verify?token=t", 24).text
        assert "<" not in text
        assert "Baskfy" in text
