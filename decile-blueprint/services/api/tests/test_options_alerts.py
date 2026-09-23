"""The options book's five Prometheus rules (OP14, ``docs/options/05`` §4): each exists, reads a
gauge the API publishes, points at runbook 12, and **fires from a synthetic series** inside its
window and not outside — evaluated by ``test_swing_alerts.fires``, which raises on any clause
outside the grammar it understands. The gauges' values from real rows are
``services/worker/tests/test_options_health_checks.py``'s.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Final, cast

import pytest
import yaml
from prometheus_client import generate_latest
from test_swing_alerts import IST, SATURDAY, WEDNESDAY, fires

from baskfy_api import metrics
from baskfy_worker.alerts import AlertName

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
ALERTS_FILE: Final = REPO_ROOT / "infra" / "prometheus" / "alerts.yml"
RUNBOOK: Final = "docs/runbooks/12-options-health.md"
OPTIONS_RULES: Final = (
    "OPTIONS_OPEN_AFTER_HARD_EXIT",
    "OPTIONS_COLLECTOR_GAP",
    "OPTIONS_SCAN_STALE",
    "OPTIONS_LIMITER_SHARE_HIGH",
    "OPTIONS_NO_SESSION_ON_TRADING_DAY",
)
HEALTHY: Final[dict[str, float]] = {
    "baskfy_options_open_after_hard_exit": 0,
    "baskfy_options_collector_gap_minutes": 0,
    "baskfy_options_scan_stale_minutes": 0,
    "baskfy_options_limiter_share_pct": 2,
    "baskfy_options_sessions_today": 1,
    "baskfy_options_trading_day": 1,
    "baskfy_options_collect_enabled": 1,
    "baskfy_options_scan_enabled": 1,
    "baskfy_options_monitor_enabled": 1,
}
#: Per rule: what goes wrong, an IST time inside its window, and one outside.
SCENARIOS: Final[dict[str, tuple[dict[str, float], str, str]]] = {
    "OPTIONS_OPEN_AFTER_HARD_EXIT": ({"baskfy_options_open_after_hard_exit": 1}, "14:33", "14:33"),
    "OPTIONS_COLLECTOR_GAP": ({"baskfy_options_collector_gap_minutes": 5}, "11:00", "09:18"),
    "OPTIONS_SCAN_STALE": ({"baskfy_options_scan_stale_minutes": 4}, "13:00", "15:31"),
    "OPTIONS_LIMITER_SHARE_HIGH": ({"baskfy_options_limiter_share_pct": 40}, "10:00", "10:00"),
    "OPTIONS_NO_SESSION_ON_TRADING_DAY": ({"baskfy_options_sessions_today": 0}, "10:25", "10:15"),
}


def _rules() -> dict[str, dict[str, object]]:
    document = yaml.safe_load(ALERTS_FILE.read_text(encoding="utf-8"))
    group = next(g for g in document["groups"] if g["name"] == "baskfy-options")
    return {str(rule["alert"]): rule for rule in group["rules"]}


def _ist(day: dt.date, hhmm: str) -> dt.datetime:
    hour, minute = (int(x) for x in hhmm.split(":"))
    return dt.datetime.combine(day, dt.time(hour, minute), tzinfo=IST).astimezone(dt.UTC)


@pytest.fixture(scope="module")
def rules() -> dict[str, dict[str, object]]:
    return _rules()


class TestTheFiveRulesExist:
    @pytest.mark.parametrize("name", OPTIONS_RULES)
    def test_each_rule_exists_is_an_alert_name_and_names_runbook_12(
        self, rules: dict[str, dict[str, object]], name: str
    ) -> None:
        assert name in rules
        assert AlertName(name).value == name
        assert cast(dict[str, str], rules[name]["labels"])["service"] == "options"
        path = cast(dict[str, str], rules[name]["annotations"])["runbook_url"]
        assert path == RUNBOOK and name in (REPO_ROOT / path).read_text(encoding="utf-8")

    def test_the_rules_read_exactly_the_published_gauges(
        self, rules: dict[str, dict[str, object]]
    ) -> None:
        for gauge in (
            metrics.OPTIONS_OPEN_AFTER_HARD_EXIT, metrics.OPTIONS_COLLECTOR_GAP_MINUTES,
            metrics.OPTIONS_SCAN_STALE_MINUTES, metrics.OPTIONS_LIMITER_SHARE_PCT,
            metrics.OPTIONS_SESSIONS_TODAY, metrics.OPTIONS_TRADING_DAY,
            metrics.OPTIONS_COLLECT_ENABLED, metrics.OPTIONS_SCAN_ENABLED,
            metrics.OPTIONS_MONITOR_ENABLED,
        ):  # fmt: skip
            gauge.set(0)
        exposition = generate_latest(metrics.REGISTRY).decode("utf-8")
        gauges = {
            line.split(" ")[2]
            for line in exposition.splitlines()
            if line.startswith("# TYPE baskfy_options_") and line.endswith(" gauge")
        }
        read: set[str] = set()
        for name in OPTIONS_RULES:
            read.update(re.findall(r"\bbaskfy_options_[a-z_]+", str(rules[name]["expr"])))
        assert read == gauges


class TestEachRuleFiresFromASyntheticSeries:
    @pytest.mark.parametrize("name", OPTIONS_RULES)
    def test_fires_inside_its_window_and_not_outside(
        self, rules: dict[str, dict[str, object]], name: str
    ) -> None:
        wrong, inside, outside = SCENARIOS[name]
        expr = str(rules[name]["expr"])
        sick = {**HEALTHY, **wrong}
        assert fires(expr, sick, _ist(WEDNESDAY, inside)), f"{name} did not fire at {inside}"
        assert not fires(expr, HEALTHY, _ist(WEDNESDAY, inside)), f"{name} fired while healthy"
        if inside != outside:
            assert not fires(expr, sick, _ist(WEDNESDAY, outside)), f"{name} fired at {outside}"
            assert not fires(expr, sick, _ist(SATURDAY, inside)), f"{name} fired on a Saturday"

    @pytest.mark.parametrize(
        ("name", "flag"),
        [
            ("OPTIONS_COLLECTOR_GAP", "baskfy_options_collect_enabled"),
            ("OPTIONS_SCAN_STALE", "baskfy_options_scan_enabled"),
            ("OPTIONS_NO_SESSION_ON_TRADING_DAY", "baskfy_options_monitor_enabled"),
            ("OPTIONS_NO_SESSION_ON_TRADING_DAY", "baskfy_options_trading_day"),
        ],
    )
    def test_quiet_with_its_switch_off_or_on_a_holiday(
        self, rules: dict[str, dict[str, object]], name: str, flag: str
    ) -> None:
        wrong, inside, _ = SCENARIOS[name]
        off = {**HEALTHY, **wrong, flag: 0}
        assert not fires(str(rules[name]["expr"]), off, _ist(WEDNESDAY, inside))
