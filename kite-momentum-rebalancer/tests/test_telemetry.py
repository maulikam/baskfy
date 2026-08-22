"""Observability over the order path — M20 §1.

Two properties, and the second matters more than the first.

1. When it is configured, the order path is traceable end to end.
2. **When it is not, nothing changes.** The Mumbai box runs a live trading desk with no collector,
   no Sentry client and none of the three libraries installed. Observability must never be the
   reason an order does not get placed, so every helper has to be a no-op that cannot raise.
"""

from __future__ import annotations

import inspect

import pytest

from app import telemetry


class TestItDegradesToNothing:
    """The branch that runs in production today."""

    def test_span_yields_and_does_not_raise_with_no_tracer(self) -> None:
        with telemetry.span("anything", a="b") as current:
            assert current is None

    def test_counters_are_silent_when_metrics_are_off(self) -> None:
        telemetry.count("orders", action="BUY", outcome="COMPLETE")   # must simply return

    def test_histograms_are_silent_when_metrics_are_off(self) -> None:
        telemetry.observe("execute_seconds", 1.25)

    def test_an_unknown_metric_name_is_not_an_error(self) -> None:
        """A typo in a metric name must not take down a rebalance."""
        telemetry.count("no_such_metric", x="y")
        telemetry.observe("no_such_metric", 1.0)

    def test_capture_is_silent_without_a_dsn(self) -> None:
        telemetry.capture(ValueError("boom"), plan_id="p1")

    def test_a_broken_tracer_does_not_break_the_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If the tracer itself throws, the work still has to happen."""

        class _Exploding:
            def start_as_current_span(self, *a: object, **k: object) -> object:
                raise RuntimeError("collector unreachable")

        monkeypatch.setattr(telemetry, "_tracer", _Exploding())
        ran = False
        with telemetry.span("desk.execute"):
            ran = True
        assert ran, "a tracer failure aborted the work it was supposed to observe"

    def test_a_broken_counter_does_not_break_the_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Exploding:
            def labels(self, **k: object) -> object:
                raise RuntimeError("registry is confused")

        monkeypatch.setitem(telemetry._metrics, "orders", _Exploding())
        telemetry.count("orders", action="BUY", outcome="COMPLETE")


class TestItRecordsShapeNotContent:
    """A span attribute travels to a third party. The desk's journal is the local record."""

    def test_no_price_quantity_or_token_is_ever_an_attribute(self) -> None:
        from app import main

        source = inspect.getsource(main)
        instrumented = [
            line for line in source.splitlines()
            if "_tel.span(" in line or "_tel.count(" in line or "_tel.observe(" in line
        ]
        assert instrumented, "the order path is not instrumented at all"
        for line in instrumented:
            for forbidden in ("price", "ref_price", "qty", "quantity", "nav", "token", "cash"):
                assert forbidden not in line.lower(), f"{forbidden!r} must not leave the desk: {line}"

    def test_the_metrics_that_exist_are_the_ones_alerts_reference(self) -> None:
        """Every metric the install path defines is one the alert rules can evaluate over."""
        expected = {"plans", "orders", "refusals", "gtt", "execute_seconds", "batch_size"}
        source = inspect.getsource(telemetry.install)
        for name in expected:
            assert f'"{name}"' in source


class TestTheOrderPathIsInstrumented:
    """plan -> execute -> GTT, the three stages M20 names."""

    @pytest.mark.parametrize(
        ("marker", "stage"),
        [
            ('_tel.span("desk.plan.build"', "plan"),
            ('_tel.count("orders"', "execute"),
            ('_tel.count("gtt"', "GTT"),
            ('_tel.observe("execute_seconds"', "execute timing"),
            ("_tel.capture(", "error capture"),
        ],
    )
    def test_each_stage_is_covered(self, marker: str, stage: str) -> None:
        from app import main

        assert marker in inspect.getsource(main), f"{stage} is not instrumented"

    def test_install_reports_what_actually_came_up(self) -> None:
        """Not what was configured — what started. A DSN set against a missing library is off."""
        result = telemetry.install()
        assert set(result) == {"traces", "metrics", "errors"}
        assert all(isinstance(v, bool) for v in result.values())


def test_nothing_here_can_place_an_order() -> None:
    source = inspect.getsource(telemetry)
    for forbidden in ("place_order", "OrderGateway", "kiteconnect", "place_gtt"):
        assert forbidden not in source
