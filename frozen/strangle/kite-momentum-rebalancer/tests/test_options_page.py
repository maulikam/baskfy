"""The options page — the only window onto a paper-only program.

Its whole value is being believed on two points: that nothing has been traded, and that
the sample is nowhere near decisive. Both are derived here rather than asserted, and these
tests exist to keep them derived.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import app.main as M
from app.analytics import db, options_view as OV
from app.strategies import options_experiment as X


@pytest.fixture()
def conn(tmp_path):
    with db.connect(str(tmp_path / "p.db")) as c:
        db.migrate(c)
        yield c


@pytest.fixture()
def client():
    return TestClient(M.app)


def journal(tmp_path, *records):
    p = tmp_path / "journal.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in records))
    return str(p)


# =====================================================================================
# the paper-only claim must be evidence, not intent
# =====================================================================================
def test_an_empty_journal_reports_no_orders(conn, tmp_path):
    assert OV.page(conn, journal=journal(tmp_path))["orders"]["placed"] == 0


def test_an_option_order_in_the_journal_is_counted(conn, tmp_path):
    """If this ever fires, the paper-only assumption is false and the page must say so
    rather than keep displaying a reassuring banner."""
    j = journal(tmp_path,
                {"event": "placed", "symbol": "NIFTY2681824350CE", "order_id": "1"},
                {"event": "dry_run", "symbol": "NIFTY2681824350PE"})
    assert OV.page(conn, journal=j)["orders"]["placed"] == 2


def test_equity_orders_are_not_counted_as_option_orders(conn, tmp_path):
    """RELIANCE ends in CE. The whole count would be wrong if is_option() were naive."""
    j = journal(tmp_path,
                {"event": "placed", "symbol": "RELIANCE"},
                {"event": "placed", "symbol": "TITAN"})
    assert OV.page(conn, journal=j)["orders"]["placed"] == 0


def test_a_guard_refusal_is_counted_separately_from_a_placement(conn, tmp_path):
    """A refusal is the system working, not the system trading."""
    j = journal(tmp_path, {"event": "overnight_option_block",
                           "symbol": "NIFTY2681824350CE", "product": "NRML"})
    p = OV.page(conn, journal=j)["orders"]
    assert (p["placed"], p["blocked"]) == (0, 1)


def test_a_missing_journal_is_reported_rather_than_read_as_zero(conn, tmp_path):
    p = OV.page(conn, journal=str(tmp_path / "nope.jsonl"))["orders"]
    assert p["journal_present"] is False


# =====================================================================================
# the sample denominator
# =====================================================================================
def test_the_count_is_always_shown_against_what_would_be_decisive(conn):
    s = OV.page(conn)["sample"]
    assert s["have"] == 0
    assert s["need_low"] >= 500 and s["need_high"] > s["need_low"]
    assert s["sessions_remaining"] == s["need_low"]


def test_the_posture_reports_the_hard_coded_carry_block(conn):
    """Not read from config, because it is not in config — that is the point."""
    g = OV.page(conn)["gates"]
    assert set(g["carry_products_blocked"]) == {"NRML", "CNC"}


# =====================================================================================
# replayability
# =====================================================================================
def test_an_arm_without_tokens_is_flagged_as_unreplayable(conn):
    vid = X.register(conn, X.Variant("wing05", X.INTRADAY, {"wing_delta": 0.05}))
    import datetime as dt
    for contracts in ([], [{"symbol": "NIFTY2681824350CE", "token": 1, "side": "SELL",
                            "quantity": 65}]):
        X.open_arm(conn, variant_id=vid, strategy="seller", arm=X.INTRADAY,
                   expiry=dt.date(2026, 8, 18), lots=1, lot_size=65,
                   entry_fills=[X.executable_fill("SELL", 30.0, 30.1, 65, "a")],
                   entry_at=dt.datetime(2026, 8, 17, 9, 45), entry_spot=24400.0,
                   contracts=contracts)
    t = OV.page(conn)["tokens"]
    assert (t["with_tokens"], t["without"], t["total"]) == (1, 1, 2)


# =====================================================================================
# the page itself
# =====================================================================================
def test_the_page_needs_no_kite_session(client):
    """It answers 'what is this program doing', which must be answerable when logged out —
    otherwise it is unavailable exactly when someone wonders."""
    assert client.get("/options").status_code == 200


def test_the_page_says_nothing_has_been_traded(client):
    body = client.get("/options").text
    assert "No option order has ever been placed" in body
    assert "paper only" in body.lower()


def test_the_page_states_the_sample_it_would_need(client):
    assert "550" in client.get("/options").text


def test_the_page_explains_why_the_overnight_arm_is_absent(client):
    """A missing arm with no explanation reads as an oversight; this one was a decision."""
    # Whitespace-normalised: the phrase wraps across lines in the template, and a test
    # that only passes on one line-wrapping is a test of the formatter.
    body = " ".join(client.get("/options").text.split())
    assert "risk decision, not a result" in body


def test_every_page_links_to_it(client):
    for path in ("/", "/stops", "/performance", "/tradebook", "/settings", "/ops"):
        assert '/options"' in client.get(path).text, path


# =====================================================================================
# test isolation — the reason this file can trust the journal at all
# =====================================================================================
def test_the_suite_cannot_write_to_the_production_journal():
    """A run once left 2,434 synthetic orders in the real journal, which is the record the
    page reads to prove nothing was traded. An audit file containing events that did not
    happen cannot serve as evidence."""
    import app.core.gateway as gateway
    assert "data/outputs/orders_journal.jsonl" not in gateway.JOURNAL


# =====================================================================================
# the strangle run controls
# =====================================================================================
def test_the_page_offers_every_strangle_control(client):
    from app.analytics import options_view as OV
    body = client.get("/options").text
    for name in OV.STRANGLE_OPS:
        assert f'value="{name}"' in body, name


def test_the_controls_post_an_operation_name_never_a_command(client):
    """The same fixed allowlist as /ops. A page that can run an arbitrary string is remote
    code execution on the machine holding the broker credentials."""
    import re
    from app.analytics import options_view as OV
    body = client.get("/options").text
    assert 'action="/options/run"' in body
    # Every op the page can submit is on the allowlist, and there is no free-form field to
    # type a command into. ("shell" as a substring is not a security check — it matched a
    # CSS comment about the inherited page shell.)
    posted = set(re.findall(r'name="op" value="([^"]+)"', body))
    assert posted and posted <= set(OV.STRANGLE_OPS)
    assert not re.search(r'<(input|textarea)[^>]*name="(cmd|command|argv)"', body)


def test_the_run_route_refuses_an_operation_outside_the_options_group(client):
    """Restricted further than /ops: this route cannot start the equity jobs even if the
    form is edited in the browser."""
    from urllib.parse import unquote_plus
    r = client.post("/options/run", data={"op": "daily"}, follow_redirects=False)
    assert r.status_code == 303
    assert "is not an options operation" in unquote_plus(r.headers["location"])


def test_the_run_route_refuses_an_unknown_operation(client):
    r = client.post("/options/run", data={"op": "rm -rf /"}, follow_redirects=False)
    assert r.status_code == 303 and "error=" in r.headers["location"]


def test_every_strangle_operation_is_paper_only():
    """None of them can reach a live order: the scripts contain no order call and the
    seven locks are shut regardless."""
    from app.analytics import ops, options_view as OV
    for name in OV.STRANGLE_OPS:
        argv = ops.BY_NAME[name].argv({})
        assert argv[1] == "-m" and argv[2].startswith("scripts.strangle")


def test_the_paper_session_is_bounded_so_it_cannot_hold_the_shared_lock_all_day():
    """Operations share one lock. An unbounded session would run to 15:10 and block the
    equity daily job behind it."""
    from app.analytics import ops
    assert "--max-ticks" in ops.BY_NAME["strangle_session"].cli()


def test_the_page_names_the_single_next_action(client):
    """'Why can I not trade' is one question and deserves one answer at a time, not seven
    switches to reason about."""
    body = " ".join(client.get("/options").text.split())
    assert "next step" in body.lower()
    assert "Record today&#39;s straddle" in body or "Record today's straddle" in body


def test_the_strangle_view_needs_no_kite_session():
    from app.analytics import options_view as OV
    s = OV.strangle()
    assert s["available"] and s["mode"] == "paper"
    assert s["paper_needed"] == 60
