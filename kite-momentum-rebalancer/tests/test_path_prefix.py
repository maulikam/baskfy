"""The desk mounted at /desk on the web app's address (Maulik, 29 Oct 2026).

"i want both the app and desk in same, no two different port and applications". Caddy strips
`/desk` on the way in; `app.core.path_prefix` must put it back on every URL the desk hands the
browser, or a click on "Swing" lands in the web app's /swing instead of the desk's.

The guarantee that matters most is the first test: with the setting empty, nothing changes.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.testclient import TestClient

from app.core.path_prefix import PathPrefix, normalise, rewrite_body, rewrite_location

PAGE = """<a href="/swing">Swing</a> <a href='/ops'>Ops</a> <form action="/execute" method="post">
<link href="/static/app.css"> <img src="/static/logo.svg"> <a href="https://kite.zerodha.com/x">K</a>
<a href="//cdn.example/x">cdn</a> <a href="/desk/already">done</a> <a href="#top">top</a>
<script>fetch('/analyze', {method: 'POST'}); fetch(`/swing/data`); fetch("/vbt/rescan")</script>"""


def _app(prefix: str) -> TestClient:
    inner = FastAPI()

    @inner.get("/page")
    def page() -> HTMLResponse:
        return HTMLResponse(PAGE)

    @inner.get("/go")
    def go() -> RedirectResponse:
        return RedirectResponse("/tradebook?imported=3", status_code=303)

    @inner.get("/data")
    def data() -> JSONResponse:
        return JSONResponse({"href": "/swing", "amount": "1000.00"})

    @inner.get("/text")
    def text() -> PlainTextResponse:
        return PlainTextResponse('href="/swing"')

    inner.add_middleware(PathPrefix, prefix=prefix)
    return TestClient(inner)


def test_empty_prefix_leaves_every_byte_alone():
    client = _app("")
    assert client.get("/page").text == PAGE
    assert client.get("/go", follow_redirects=False).headers["location"] == "/tradebook?imported=3"


def test_links_forms_assets_and_fetches_are_prefixed():
    body = _app("/desk").get("/page").text
    for url in ('href="/desk/swing"', "href='/desk/ops'", 'action="/desk/execute"',
                'href="/desk/static/app.css"', 'src="/desk/static/logo.svg"',
                "fetch('/desk/analyze'", "fetch(`/desk/swing/data`", 'fetch("/desk/vbt/rescan"'):
        assert url in body, url


def test_external_protocol_relative_fragment_and_already_prefixed_urls_are_untouched():
    body = _app("/desk").get("/page").text
    assert 'href="https://kite.zerodha.com/x"' in body
    assert 'href="//cdn.example/x"' in body
    assert 'href="/desk/already"' in body and "/desk/desk/" not in body
    assert 'href="#top"' in body


def test_redirects_are_prefixed():
    response = _app("/desk").get("/go", follow_redirects=False)
    assert response.headers["location"] == "/desk/tradebook?imported=3"


def test_content_length_matches_the_rewritten_body():
    response = _app("/desk").get("/page")
    assert int(response.headers["content-length"]) == len(response.content)


def test_json_and_plain_text_are_never_rewritten():
    client = _app("/desk")
    assert client.get("/data").json() == {"href": "/swing", "amount": "1000.00"}
    assert client.get("/text").text == 'href="/swing"'


def test_prefix_spellings_normalise():
    assert normalise("desk") == normalise("/desk/") == normalise(" /desk ") == "/desk"
    assert normalise("") == normalise("/") == ""


def test_location_rules():
    assert rewrite_location("/stops?armed=1", "/desk") == "/desk/stops?armed=1"
    assert rewrite_location("/desk", "/desk") == "/desk"
    assert rewrite_location("https://kite.trade/connect", "/desk") == "https://kite.trade/connect"
    assert rewrite_location("//x.example/", "/desk") == "//x.example/"


def test_a_root_path_that_merely_starts_with_the_prefix_word_is_still_prefixed():
    assert rewrite_body('href="/desktop"', "/desk") == 'href="/desk/desktop"'
