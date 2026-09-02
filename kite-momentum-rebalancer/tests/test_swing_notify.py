"""The swing notifier (SW11, STANDING-ANSWERS A2): tells, never acts, never stops the morning."""
from __future__ import annotations

import ast
import datetime as dt
import inspect
import re
from decimal import Decimal

import pytest

from app import config as C
from app import swing_monitor, swing_notify
from app.strategies.swing_breakout import Signal, WatchedName
from app.swing_notify import (
    EmailSender,
    Notifier,
    SignalNotice,
    TelegramSender,
    build_senders,
)
from baskfy_core.swing.config import DEFAULT_SWING_CONFIG, Setup
from baskfy_core.swing.opening_range import TriggerState, TriggerVerdict
from tests.test_swing_monitor import FakeConn

D = Decimal
AT = dt.datetime(2026, 8, 19, 9, 31)


def _notice(**over) -> SignalNotice:
    base = dict(
        symbol="ALPHAFLAG", setup="FLAG", at=AT, entry=D("100.80"), stop=D("97.80"),
        quantity=1666, risk_inr=D("4998.00"), plan_expires_at=AT + dt.timedelta(minutes=30),
    )
    base.update(over)
    return SignalNotice(**base)


class FakeSMTP:
    """What `smtplib.SMTP` is used as: a context manager with starttls/login/send_message."""

    sent: list = []
    calls: list[str] = []

    def __init__(self, host, port, timeout=None):
        FakeSMTP.calls.append(f"connect {host}:{port} timeout={timeout}")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        FakeSMTP.calls.append("starttls")

    def login(self, user, password):
        FakeSMTP.calls.append(f"login {user}")

    def send_message(self, message):
        FakeSMTP.sent.append(message)


class TestTheEmail:
    def test_email_signal_carries_the_whole_line(self):
        """A2: symbol, entry, stop, qty, ₹ risk, plan expiry — all of it, in one message."""
        FakeSMTP.sent, FakeSMTP.calls = [], []
        sender = EmailSender(host="relay", port=587, user="u", password="p", sender="desk@x",
                             to="maulik@x", smtp=FakeSMTP)
        assert sender.send(_notice().subject(), _notice().text()) is True
        message = FakeSMTP.sent[0]
        body = message.get_content()
        assert message["To"] == "maulik@x" and message["From"] == "desk@x"
        assert "ALPHAFLAG" in message["Subject"] and "09:31" in message["Subject"]
        for needle in ("100.80", "97.80", "1666", "₹4,998.00", "expires 10:01", "DRY_RUN"):
            assert needle in body, f"{needle!r} missing from the email"
        assert FakeSMTP.calls == ["connect relay:587 timeout=5.0", "starttls", "login u"]

    def test_email_signal_for_a_skipped_trigger_says_why(self):
        text = _notice(quantity=None, risk_inr=None, plan_expires_at=None,
                       skipped="GATE_RED gate is RED").text()
        assert "No line: GATE_RED gate is RED." in text
        assert "skipped (GATE_RED gate is RED)" in _notice(skipped="GATE_RED gate is RED").subject()

    def test_email_signal_failure_is_false_not_an_exception(self):
        class Refusing(FakeSMTP):
            def __init__(self, host, port, timeout=None):
                raise OSError("relay down")

        sender = EmailSender(host="relay", port=587, sender="a", to="b", smtp=Refusing)
        assert sender.send("s", "t") is False


class TestTelegramIsDark:
    def test_telegram_sender_is_not_constructed_unless_both_are_set(self, monkeypatch):
        monkeypatch.setattr(C, "DESK_SMTP_HOST", "")
        monkeypatch.setattr(C, "SWING_TELEGRAM_BOT_TOKEN", "123:abc")
        monkeypatch.setattr(C, "SWING_TELEGRAM_CHAT_ID", "")
        assert build_senders() == []
        monkeypatch.setattr(C, "SWING_TELEGRAM_BOT_TOKEN", "")
        monkeypatch.setattr(C, "SWING_TELEGRAM_CHAT_ID", "42")
        assert build_senders() == []
        monkeypatch.setattr(C, "SWING_TELEGRAM_BOT_TOKEN", "123:abc")
        assert [s.name for s in build_senders()] == ["telegram"]
        with pytest.raises(ValueError):
            TelegramSender(token="", chat_id="42")

    def test_the_process_default_is_no_channel_at_all(self):
        """Both trees' .env.example leave every knob blank: a fresh desk pushes nothing."""
        assert C.SWING_TELEGRAM_BOT_TOKEN == "" and C.SWING_TELEGRAM_CHAT_ID == ""
        assert C.DESK_SMTP_HOST == ""
        assert build_senders() == []

    def test_telegram_send_is_one_post_to_send_message_and_fails_soft(self):
        seen: list[tuple[str, bytes]] = []

        class Opened:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def opener(request, timeout):
            seen.append((request.full_url, request.data))
            return Opened()

        sender = TelegramSender(token="123:abc", chat_id="42", opener=opener)
        assert sender.send("subj", "body") is True
        assert seen[0][0] == "https://api.telegram.org/bot123:abc/sendMessage"
        assert b"chat_id=42" in seen[0][1] and b"subj" in seen[0][1]

        def failing(request, timeout):
            raise OSError("no network")

        assert TelegramSender(token="1:a", chat_id="2", opener=failing).send("s", "t") is False

    def test_the_notifier_module_names_no_confirm_path(self):
        """The source scan: no execute route, no gateway, no broker client, no reading back."""
        tree = ast.parse(inspect.getsource(swing_notify))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                body = node.body
                if body and isinstance(body[0], ast.Expr) and isinstance(
                    getattr(body[0], "value", None), ast.Constant
                ):
                    node.body = body[1:] or [ast.Pass()]
        code = ast.unparse(tree)
        for pattern in (r"execute", r"gateway", r"\bplace", r"confirm", r"\bkc\b", r"kiteconnect",
                        r"kite_client", r"getUpdates", r"setWebhook", r"swing_execute",
                        r"swing_desk", r"\border\b"):
            assert not re.search(pattern, code, re.I), f"swing_notify.py names {pattern}"
        imported = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        # The one relative import is `config`; nothing from the execution side of the desk.
        relative = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level]
        assert [alias.name for n in relative for alias in n.names] == ["config"]
        assert not any("execution" in m or "gateway" in m or "kite" in m for m in imported)


class RaisingSender:
    name = "boom"

    def send(self, subject, text):
        raise RuntimeError("channel exploded")


class FlakySender:
    name = "flaky"

    def send(self, subject, text):
        return False


class Recording:
    name = "rec"

    def __init__(self):
        self.messages: list[tuple[str, str]] = []

    def send(self, subject, text):
        self.messages.append((subject, text))
        return True


def _watch(symbol: str, token: int, *, focus: bool) -> WatchedName:
    return WatchedName(watch_id=token, instrument_id=token * 10, symbol=symbol, token=token,
                       setup=Setup.FLAG, pivot_high=D("100"), upper_circuit=None, focus=focus)


def _triggered(watch: WatchedName) -> Signal:
    verdict = TriggerVerdict(TriggerState.TRIGGERED, D("100.80"), D("97.80"), D("99.5"), D("98.0"))
    return Signal(watch=watch, at=AT, verdict=verdict, last_price=D("100.80"),
                  low_of_day=D("97.80"), window_minutes=5)


class TestTheNotifierNeverStopsTheMonitor:
    def test_a_notifier_failure_never_stops_the_monitor(self):
        """The channel raises; the signal row, the plan and the line are still written."""
        conn = FakeConn(stats={"AAA": ("5.00", 100_000_000, "72.00")})
        store = swing_monitor.PgSignalStore(
            conn, user_id=1, day=AT.date(), config=DEFAULT_SWING_CONFIG,
            notifier=Notifier([RaisingSender()]),
        )
        store.raise_signal(_triggered(_watch("AAA", 1, focus=True)))
        assert len(conn.inserted("sw_signal")) == 1
        assert len(conn.inserted("sw_plan")) == 1 and len(conn.inserted("sw_plan_line")) == 1
        assert store.notices and store.notices[0].symbol == "AAA"
        assert store.notifier.sent == [("boom", store.notices[0].subject(), False)]

    def test_notifier_counts_a_channel_that_answers_false_as_not_sent(self):
        rec = Recording()
        notifier = Notifier([FlakySender(), rec])
        assert notifier.notify(_notice()) == 1
        assert [ok for _, _, ok in notifier.sent] == [False, True]
        assert rec.messages[0][0].startswith("[swing] ALPHAFLAG triggered at 09:31")

    def test_only_daily_focus_names_are_notified(self):
        """A14: the top five plus every EP are pushed; the other fifteen are the row and the
        line below the fold, never a push."""
        rec = Recording()
        conn = FakeConn(stats={"AAA": ("5.00", 100_000_000, "72.00"),
                               "BBB": ("5.00", 100_000_000, "61.00")})
        store = swing_monitor.PgSignalStore(
            conn, user_id=1, day=AT.date(), config=DEFAULT_SWING_CONFIG, notifier=Notifier([rec]),
        )
        store.raise_signal(_triggered(_watch("BBB", 2, focus=False)))
        assert rec.messages == [] and store.notices == []
        assert len(conn.inserted("sw_plan_line")) == 1  # the line is still written
        store.raise_signal(_triggered(_watch("AAA", 1, focus=True)))
        assert len(rec.messages) == 1 and "AAA" in rec.messages[0][0]
        body = rec.messages[0][1]
        assert "qty     1666" in body and "₹4,998.00" in body and "expires 10:01" in body

    def test_a_focus_trigger_that_is_skipped_is_told_with_the_reason(self):
        rec = Recording()
        conn = FakeConn(gate="RED", stats={"AAA": ("5.00", 100_000_000, "72.00")})
        store = swing_monitor.PgSignalStore(
            conn, user_id=1, day=AT.date(), config=DEFAULT_SWING_CONFIG, notifier=Notifier([rec]),
        )
        store.raise_signal(_triggered(_watch("AAA", 1, focus=True)))
        assert len(conn.inserted("sw_plan_line")) == 0
        assert "skipped (GATE_RED" in rec.messages[0][0]
        assert store.notices[0].quantity is None

    def test_notifier_observe_sink_that_raises_is_dropped(self):
        def sink(channel, outcome):
            raise RuntimeError("metrics down")

        notifier = Notifier([Recording()], observe=sink)
        assert notifier.notify(_notice()) == 1
