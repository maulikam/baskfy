"""The five swing alert rules (SW11, STANDING-ANSWERS B8): each exists, reads a metric the API
publishes, points at a runbook that exists, and **fires from a synthetic series** — evaluated
here, because `promtool` is not on the locked stack and a rule nobody has evaluated is a rule
that may never fire.

The evaluator understands exactly the grammar the swing rules are written in — a gauge
comparison, `and on()` conjunctions, an IST window written as UTC minutes-of-day, a weekday
guard — and raises on anything else, so a rule that drifts outside it fails this file loudly
rather than passing unevaluated.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Final, cast

import pytest
import yaml
from prometheus_client import generate_latest

from baskfy_api import metrics
from baskfy_worker.alerts import AlertName

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
ALERTS_FILE: Final = REPO_ROOT / "infra" / "prometheus" / "alerts.yml"

SWING_RULES: Final = (
    "SWING_POSITION_NAKED",
    "SWING_MONITOR_DID_NOT_START",
    "SWING_DETECT_STALE",
    "SWING_ORDER_OPEN_AFTER_CUTOFF",
    "SWING_GTT_MISSING_AT_1515",
)

IST: Final = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _rules() -> dict[str, dict[str, object]]:
    document = yaml.safe_load(ALERTS_FILE.read_text(encoding="utf-8"))
    group = next(g for g in document["groups"] if g["name"] == "baskfy-swing")
    return {str(rule["alert"]): rule for rule in group["rules"]}


# --- a PromQL-subset evaluator -------------------------------------------------------------

_GAUGE = re.compile(r"^(baskfy_swing_[a-z_]+)\s*(==|>|<|>=|<=)\s*(-?\d+)$")
_CLOCK = re.compile(r"^\(hour\(\) \* 60 \+ minute\(\)\)\s*(>=|<)\s*(\d+)$")
_WEEKDAY = "(day_of_week() > 0 and day_of_week() < 6)"
_OPS = {
    "==": lambda a, b: a == b,
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
}


def fires(expr: str, series: dict[str, float], at_utc: dt.datetime) -> bool:
    """Evaluate one swing rule at one instant against one set of gauge values."""
    clauses = [c.strip() for c in " ".join(expr.split()).split(" and on() ")]
    minute_of_day = at_utc.hour * 60 + at_utc.minute
    # PromQL's day_of_week(): 0 = Sunday … 6 = Saturday; Python's weekday(): 0 = Monday.
    prom_dow = (at_utc.weekday() + 1) % 7
    for clause in clauses:
        if clause == _WEEKDAY:
            if not (0 < prom_dow < 6):
                return False
            continue
        if (m := _CLOCK.match(clause)) is not None:
            if not _OPS[m.group(1)](minute_of_day, int(m.group(2))):
                return False
            continue
        if (m := _GAUGE.match(clause)) is not None:
            name = m.group(1)
            if name not in series:
                raise AssertionError(f"the rule reads {name}, which the series does not carry")
            if not _OPS[m.group(2)](series[name], int(m.group(3))):
                return False
            continue
        raise AssertionError(f"clause outside the evaluator's grammar: {clause!r}")
    return True


def _ist(day: dt.date, hhmm: str) -> dt.datetime:
    hour, minute = (int(x) for x in hhmm.split(":"))
    return dt.datetime.combine(day, dt.time(hour, minute), tzinfo=IST).astimezone(dt.UTC)


WEDNESDAY: Final = dt.date(2026, 9, 2)
SATURDAY: Final = dt.date(2026, 9, 5)

#: Everything healthy: nothing naked, the flag on and the monitor running, detect done, no
#: open order.
HEALTHY: Final[dict[str, float]] = {
    "baskfy_swing_naked_positions": 0,
    "baskfy_swing_monitor_enabled": 1,
    "baskfy_swing_monitor_ran_today": 1,
    "baskfy_swing_open_buy_orders_today": 0,
    "baskfy_swing_detect_ran_for_published_date": 1,
}

#: Per rule: the one fact that goes wrong, the IST time inside its window, and a time outside.
SCENARIOS: Final[dict[str, tuple[dict[str, float], str, str]]] = {
    "SWING_POSITION_NAKED": ({"baskfy_swing_naked_positions": 2}, "11:00", "11:00"),
    "SWING_MONITOR_DID_NOT_START": ({"baskfy_swing_monitor_ran_today": 0}, "09:25", "10:50"),
    "SWING_DETECT_STALE": ({"baskfy_swing_detect_ran_for_published_date": 0}, "21:45", "21:00"),
    "SWING_ORDER_OPEN_AFTER_CUTOFF": (
        {"baskfy_swing_open_buy_orders_today": 1},
        "10:55",
        "10:30",
    ),
    "SWING_GTT_MISSING_AT_1515": ({"baskfy_swing_naked_positions": 1}, "15:25", "15:00"),
}


@pytest.fixture(scope="module")
def rules() -> dict[str, dict[str, object]]:
    return _rules()


class TestTheFiveRulesExist:
    @pytest.mark.parametrize("name", SWING_RULES)
    def test_swing_alert_rule_exists_and_is_an_alert_name(
        self, rules: dict[str, dict[str, object]], name: str
    ) -> None:
        assert name in rules, f"{name} has no Prometheus rule"
        assert AlertName(name).value == name
        labels = cast(dict[str, str], rules[name]["labels"])
        assert labels["service"] == "swing"

    def test_swing_alert_rules_point_at_runbook_6(
        self, rules: dict[str, dict[str, object]]
    ) -> None:
        for name in SWING_RULES:
            annotations = cast(dict[str, str], rules[name]["annotations"])
            path = annotations["runbook_url"]
            assert path == "docs/runbooks/06-swing-morning.md"
            assert (REPO_ROOT / path).is_file()
            assert name in (REPO_ROOT / path).read_text(encoding="utf-8")


class TestTheMetricsExist:
    def test_swing_alert_rules_read_metrics_the_api_publishes(
        self, rules: dict[str, dict[str, object]]
    ) -> None:
        """Every `baskfy_swing_*` name in a rule is a gauge in the registry, and every swing
        gauge is read by at least one rule — a gauge nobody reads is a fact nobody watches."""
        metrics.SWING_NAKED_POSITIONS.set(0)
        metrics.SWING_MONITOR_ENABLED.set(0)
        metrics.SWING_MONITOR_RAN_TODAY.set(0)
        metrics.SWING_OPEN_BUY_ORDERS_TODAY.set(0)
        metrics.SWING_DETECT_RAN_FOR_PUBLISHED_DATE.set(1)
        exposition = generate_latest(metrics.REGISTRY).decode("utf-8")
        gauges = {
            line.split(" ")[2]
            for line in exposition.splitlines()
            if line.startswith("# TYPE baskfy_swing_")
            and line.endswith(" gauge")
            and not line.split(" ")[2].endswith("_created")  # a histogram's own gauge
        }
        read: set[str] = set()
        for name in SWING_RULES:
            read.update(re.findall(r"\bbaskfy_swing_[a-z_]+", str(rules[name]["expr"])))
        assert read <= gauges, f"rules read unpublished metrics: {sorted(read - gauges)}"
        assert gauges <= read, f"gauges no rule reads: {sorted(gauges - read)}"


class TestEachRuleFiresFromASyntheticSeries:
    @pytest.mark.parametrize("name", SWING_RULES)
    def test_swing_alert_fires_inside_its_window_and_not_outside(
        self, rules: dict[str, dict[str, object]], name: str
    ) -> None:
        wrong, inside, outside = SCENARIOS[name]
        expr = str(rules[name]["expr"])
        sick = {**HEALTHY, **wrong}
        assert fires(expr, sick, _ist(WEDNESDAY, inside)), f"{name} did not fire at {inside} IST"
        assert not fires(expr, HEALTHY, _ist(WEDNESDAY, inside)), f"{name} fired while healthy"
        if inside != outside:
            assert not fires(expr, sick, _ist(WEDNESDAY, outside)), f"{name} fired at {outside}"
            assert not fires(expr, sick, _ist(SATURDAY, inside)), f"{name} fired on a Saturday"

    def test_swing_alert_monitor_did_not_start_is_quiet_with_the_flag_off(
        self, rules: dict[str, dict[str, object]]
    ) -> None:
        expr = str(rules["SWING_MONITOR_DID_NOT_START"]["expr"])
        off = {**HEALTHY, "baskfy_swing_monitor_enabled": 0, "baskfy_swing_monitor_ran_today": 0}
        assert not fires(expr, off, _ist(WEDNESDAY, "09:25"))

    def test_swing_alert_windows_are_the_ist_times_the_rules_are_named_for(
        self, rules: dict[str, dict[str, object]]
    ) -> None:
        """The UTC minutes in the file are IST wall-clock times; one boundary each, both sides."""
        naked = {**HEALTHY, "baskfy_swing_naked_positions": 1}
        gtt = str(rules["SWING_GTT_MISSING_AT_1515"]["expr"])
        assert fires(gtt, naked, _ist(WEDNESDAY, "15:20"))
        assert not fires(gtt, naked, _ist(WEDNESDAY, "15:19"))
        cutoff = str(rules["SWING_ORDER_OPEN_AFTER_CUTOFF"]["expr"])
        open_order = {**HEALTHY, "baskfy_swing_open_buy_orders_today": 1}
        assert fires(cutoff, open_order, _ist(WEDNESDAY, "10:50"))
        assert not fires(cutoff, open_order, _ist(WEDNESDAY, "10:49"))
        monitor = str(rules["SWING_MONITOR_DID_NOT_START"]["expr"])
        idle = {**HEALTHY, "baskfy_swing_monitor_ran_today": 0}
        assert fires(monitor, idle, _ist(WEDNESDAY, "09:20"))
        assert not fires(monitor, idle, _ist(WEDNESDAY, "09:19"))
        assert not fires(monitor, idle, _ist(WEDNESDAY, "10:45"))
        stale = str(rules["SWING_DETECT_STALE"]["expr"])
        undetected = {**HEALTHY, "baskfy_swing_detect_ran_for_published_date": 0}
        assert fires(stale, undetected, _ist(WEDNESDAY, "21:30"))
        assert not fires(stale, undetected, _ist(WEDNESDAY, "21:29"))

    def test_the_evaluator_refuses_a_rule_outside_its_grammar(self) -> None:
        with pytest.raises(AssertionError):
            fires("rate(baskfy_swing_naked_positions[5m]) > 0", HEALTHY, _ist(WEDNESDAY, "10:00"))
