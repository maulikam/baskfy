"""Which address the broker sees.

Kite authorises order placement against an allowlist of IPs. On 19 Aug 2026 every order
in a rebalance was refused: the connection offers both families, the resolver preferred
IPv6, and the allowlist held the IPv4 address. Adding the v6 address is not a fix — it is
a rotating residential prefix — so the requests have to leave on the family you can name.
"""
from __future__ import annotations

import socket

import pytest

from app import config as C
from app.core import net


@pytest.fixture(autouse=True)
def _restore():
    """The patch is process-global, so put it back however the test ends."""
    import urllib3.util.connection as conn
    original = conn.allowed_gai_family
    yield
    conn.allowed_gai_family = original


def test_pinning_selects_the_ipv4_family():
    import urllib3.util.connection as conn
    net.force_ipv4()
    assert conn.allowed_gai_family() == socket.AF_INET


def test_pinning_twice_changes_nothing():
    """Several entry points construct a Kite client; applying it repeatedly must be a
    no-op rather than stacking patches."""
    assert net.force_ipv4() in (True, False)
    assert net.force_ipv4() is False


def test_a_failure_to_patch_is_reported_not_raised(monkeypatch):
    """An order refused for the wrong address is a legible error. An app that will not
    start is not."""
    import builtins
    real = builtins.__import__

    def boom(name, *a, **k):
        if name == "urllib3.util.connection":
            raise ImportError("no urllib3")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", boom)
    assert net.force_ipv4() is False


def test_the_kite_client_pins_before_it_builds_a_session(monkeypatch):
    """Order matters: the session must be created after the family is fixed, or its
    connection pool resolves on the old preference."""
    calls = []
    monkeypatch.setattr(net, "force_ipv4", lambda: calls.append("pin") or True)
    import app.kite_client as KC
    monkeypatch.setattr(KC, "force_ipv4", lambda: calls.append("pin") or True)
    monkeypatch.setattr(KC, "KiteConnect", lambda **kw: calls.append("session"))
    monkeypatch.setattr(C, "FORCE_IPV4", True)
    monkeypatch.setattr(KC.Kite, "_load_token", lambda self: None)
    KC.Kite()
    assert calls == ["pin", "session"], calls


def test_the_switch_can_be_turned_off(monkeypatch):
    """It is a deliberate default, not a hardcoded rule: a host with a static IPv6 would
    want it off."""
    calls = []
    import app.kite_client as KC
    monkeypatch.setattr(KC, "force_ipv4", lambda: calls.append("pin") or True)
    monkeypatch.setattr(KC, "KiteConnect", lambda **kw: calls.append("session"))
    monkeypatch.setattr(C, "FORCE_IPV4", False)
    monkeypatch.setattr(KC.Kite, "_load_token", lambda self: None)
    KC.Kite()
    assert calls == ["session"]


def test_it_defaults_to_on():
    """The failure it prevents is every order in a batch being rejected, which is worse
    than the cost of preferring v4 on a host that did not need it."""
    assert C.FORCE_IPV4 is True
