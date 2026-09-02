"""The Prometheus surface and the alert rules that read it (Prompt 17 deliverables 2 and 3).

The tests that matter here are the two that catch the failure a metrics stack fails at silently:

* **an alert rule that names a metric nobody publishes.** It never fires, and a rule that never
  fires looks exactly like a rule that is not firing because everything is fine.
* **a metric labelled with a resolved path.** One time series per screen id takes Prometheus down,
  and it does so gradually, in production, weeks later.

Neither needs a database.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Final

import pytest
import yaml
from prometheus_client import generate_latest
from starlette.requests import Request

from baskfy_api import metrics
from baskfy_api.app import CELERY_QUEUES
from baskfy_worker.celery_app import QUEUES

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
ALERTS_FILE: Final = REPO_ROOT / "infra" / "prometheus" / "alerts.yml"
DASHBOARD_DIR: Final = REPO_ROOT / "infra" / "grafana" / "dashboards"


def _exposition() -> str:
    return generate_latest(metrics.REGISTRY).decode("utf-8")


def _touch_every_metric() -> None:
    """Observe one of everything, so each family appears in the exposition.

    An unlabelled metric with no observations is not rendered at all, so without this the parity
    tests below would report half the vocabulary as unpublished.
    """
    metrics.observe_request(method="GET", route="/t", status=200, duration_seconds=0.01)
    metrics.observe_cache(hit=True)
    metrics.observe_provider_call(provider="kite", outcome="retry")
    metrics.observe_step(step="publish", status="succeeded", duration_seconds=1.0)
    metrics.LAST_STEP_DURATION.labels("publish").set(1.0)
    metrics.LAST_STEP_ROWS.labels("publish").set(1)
    metrics.QUEUE_DEPTH.labels("ingest").set(0)
    metrics.PUBLISH_LATENCY.set(0)
    metrics.LAST_PUBLISH_AGE.set(0)
    metrics.RUNNING_RUN_AGE.set(0)
    metrics.DATA_VERSION.set(1)
    metrics.GATE_PASSED.set(1)
    metrics.ALERTS.labels("publish_late", "critical").inc()
    for status in ("running", "succeeded", "failed", "aborted"):
        metrics.RUN_STATUS.labels(status).set(0)


def _expressions(rules: str) -> list[str]:
    """Every `expr:` in the rule file, from a real YAML parse."""
    document = yaml.safe_load(rules)
    return [str(rule["expr"]) for group in document["groups"] for rule in group["rules"]]


def _recorded_rule_names(rules: str) -> set[str]:
    """The `record:` names, which later rules may legitimately reference."""
    document = yaml.safe_load(rules)
    return {
        str(rule["record"])
        for group in document["groups"]
        for rule in group["rules"]
        if "record" in rule
    }


def _metric_names() -> set[str]:
    """Every family name in the exposition, plus the histogram/counter suffixes Prometheus adds.

    A `Histogram` publishes `_bucket`, `_sum` and `_count`; a `Counter` publishes `_total`. An
    alert rule referencing `baskfy_http_requests_total` is referencing a real series even though
    the *family* is named `baskfy_http_requests`, so the suffixed forms have to be in the set or
    the parity test below would reject every correct rule.
    """
    names: set[str] = set()
    for line in _exposition().splitlines():
        if line.startswith("# TYPE "):
            _, _, family, kind = line.split(" ", 3)
            names.add(family)
            if kind == "histogram":
                names.update({f"{family}_bucket", f"{family}_sum", f"{family}_count"})
            elif kind == "counter":
                names.add(f"{family}_total")
    return names


class TestRouteLabelCardinality:
    """``route`` must be the template, never the resolved path."""

    @staticmethod
    def _request(path: str, route_path: str | None) -> Request:
        scope: dict[str, object] = {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [],
        }
        if route_path is not None:
            scope["route"] = type("Route", (), {"path": route_path})()
        return Request(scope)

    def test_a_matched_route_uses_its_template(self) -> None:
        request = self._request("/api/v1/screens/scr_abc123/run", "/api/v1/screens/{public_id}/run")
        assert metrics.route_label(request) == "/api/v1/screens/{public_id}/run"

    def test_an_unmatched_request_falls_into_one_bucket(self) -> None:
        """A 404 probe must not create a time series per URL it guessed at."""
        request = self._request("/wp-admin/setup-config.php", None)
        assert metrics.route_label(request) == metrics.UNMATCHED_ROUTE


class TestPublishLatency:
    """docs/09 §Observability: "publish latency (EOD close -> data live)"."""

    def test_the_clock_starts_at_the_1530_ist_close(self) -> None:
        """docs/DECISIONS.md §17.10: the close, not the EOD-file settle window."""
        finished = dt.datetime(2026, 8, 18, 20, 0, tzinfo=metrics.IST)
        seconds = metrics.publish_latency_seconds(dt.date(2026, 8, 18), finished)
        assert seconds == pytest.approx(4.5 * 3600)

    def test_the_2015_slo_is_four_hours_forty_five_minutes(self) -> None:
        """The number `infra/prometheus/alerts.yml`'s publish_late rule is written against."""
        deadline = dt.datetime(2026, 8, 18, 20, 15, tzinfo=metrics.IST)
        assert metrics.publish_latency_seconds(dt.date(2026, 8, 18), deadline) == 17100

    def test_a_naive_timestamp_is_read_as_utc(self) -> None:
        """asyncpg can hand back a naive datetime; reading it as local time would shift the SLO."""
        aware = dt.datetime(2026, 8, 18, 14, 30, tzinfo=dt.UTC)
        naive = aware.replace(tzinfo=None)
        assert metrics.publish_latency_seconds(
            dt.date(2026, 8, 18), naive
        ) == metrics.publish_latency_seconds(dt.date(2026, 8, 18), aware)


class TestTheExposition:
    def test_every_metric_prompt_17_names_is_published(self) -> None:
        """PROMPTS.md Prompt 17 §2 lists seven things to measure. All seven are here."""
        _touch_every_metric()
        names = _metric_names()
        for required in (
            "baskfy_pipeline_step_duration_seconds",  # pipeline step durations
            "baskfy_provider_calls_total",  # provider error rates
            "baskfy_pipeline_gate_passed",  # gate pass/fail
            "baskfy_publish_latency_seconds",  # publish latency
            "baskfy_http_request_duration_seconds",  # API latency by route
            "baskfy_screen_cache_events_total",  # cache hit rate
            "baskfy_queue_depth",  # queue depth
        ):
            assert required in names, f"{required} is not published"

    def test_the_run_status_gauge_publishes_a_zero_for_every_other_status(self) -> None:
        """A gauge that only appears on failure makes an alert that never resolves."""
        for status in ("running", "succeeded", "failed", "aborted"):
            metrics.RUN_STATUS.labels(status).set(1 if status == "failed" else 0)
        exposition = _exposition()
        for status in ("running", "succeeded", "failed", "aborted"):
            assert f'baskfy_pipeline_run_status{{status="{status}"}}' in exposition


@pytest.fixture(scope="module")
def rules() -> str:
    return ALERTS_FILE.read_text(encoding="utf-8")


class TestTheAlertRules:
    def test_every_rule_prompt_17_asks_for_exists(self, rules: str) -> None:
        """PROMPTS.md Prompt 17 §3 names six. Each is an `alert:` in the rule file."""
        for name in (
            "pipeline_failed",
            "kite_token_expiring",
            "data_quality_gate_failed",
            "publish_late",
            "api_error_rate_high",
            "queue_backlog",
        ):
            assert f"- alert: {name}" in rules, f"{name} has no Prometheus rule"

    def test_every_alert_rule_names_a_metric_we_publish(self, rules: str) -> None:
        """The test this file exists for.

        A rule watching `baskfy_pipeline_steps_total` — a plausible name we do not emit — would
        never fire, and nothing else in the system would ever say so.

        The file is **parsed**, not grepped: the comments in it name plenty of metrics, and a
        regex over the raw text would happily accept a rule whose only correct metric name was in
        a comment above it. Only `expr:` values are read, and recording rules (`decile:...`) are
        excluded because they are defined in this same file.
        """
        _touch_every_metric()
        published = _metric_names() | _recorded_rule_names(rules)

        missing = sorted(
            name
            for expression in _expressions(rules)
            for name in re.findall(r"\bbaskfy[_:][a-z0-9_:]+", expression)
            if name not in published
        )
        assert missing == [], f"alerts.yml references metrics nothing publishes: {missing}"

    def test_every_rule_points_at_a_runbook_that_exists(self, rules: str) -> None:
        """An alert whose runbook 404s is an alert with no instructions."""
        for path in set(re.findall(r"runbook_url:\s*(\S+)", rules)):
            assert (REPO_ROOT / path).is_file(), f"{path} does not exist"


class TestTheGrafanaDashboards:
    def test_both_dashboards_are_valid_json_with_stable_uids(self) -> None:
        import json  # noqa: PLC0415 - only this test needs it

        uids = set()
        for path in sorted(DASHBOARD_DIR.glob("*.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            assert document["panels"], f"{path.name} has no panels"
            uids.add(document["uid"])
        assert uids == {"baskfy-pipeline", "baskfy-service"}

    def test_every_panel_queries_a_metric_we_publish(self) -> None:
        """Same failure mode as an alert rule, one step further from anyone noticing."""
        import json  # noqa: PLC0415

        _touch_every_metric()
        published = _metric_names() | _recorded_rule_names(ALERTS_FILE.read_text(encoding="utf-8"))

        referenced: set[str] = set()
        for path in sorted(DASHBOARD_DIR.glob("*.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            for panel in document["panels"]:
                for target in panel["targets"]:
                    referenced.update(re.findall(r"\bbaskfy[_:][a-z0-9_:]+", str(target["expr"])))
        assert sorted(referenced - published) == []


def test_the_api_and_the_worker_agree_on_the_queue_names() -> None:
    """``baskfy_api.app.CELERY_QUEUES`` duplicates ``baskfy_worker.celery_app.QUEUES``.

    It has to: ``baskfy-worker`` depends on ``baskfy-api``, so the import cannot go the other way
    (the same reason ``baskfy_api.queue`` publishes by task name). This is the link that keeps the
    copy honest — without it, a fifth queue would silently never be scraped.
    """
    assert CELERY_QUEUES == QUEUES
