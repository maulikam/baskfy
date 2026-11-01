"""Dump the Python worker's Celery surface — every registered task and the Beat schedule — as JSON.

    cd decile-blueprint && PYTHONPATH=..:packages/core/src:packages/providers/src:\
packages/execution/src:services/api/src:services/worker/src .venv/bin/python \
../tools/parity/task_registry.py [--out PATH]

The answer key for ``rust/crates/baskfy-worker/tests/task_registry.rs`` (wave 6; DECISIONS-RS
R8.1): the Rust registry (``baskfy_worker::tasks::registry``) and Beat table
(``baskfy_worker::tasks::beat``) are held equal to what this prints, so a task added, renamed,
re-routed or re-scheduled in Python fails the Rust build until the port follows.

What is read is what Celery itself resolved, never the source text:

* ``app.tasks`` after ``build_celery()`` — the 76 ``baskfy.*`` names (``celery.*`` built-ins
  excluded), each task's ``run`` signature (parameter names, annotations as written, defaults),
  and the options Celery will apply: ``acks_late``, ``max_retries`` (the ``task_annotations``
  ``"*"`` entry), ``default_retry_delay``, ``autoretry_for`` (class names), ``retry_backoff``,
  ``retry_backoff_max``, ``retry_jitter``, ``time_limit``, ``soft_time_limit``, the task's own
  ``queue`` option, and whether it is bound;
* the **queue** a message lands on two ways: published by name (``send_task``, how the API and
  the Rust producer publish — ``TASK_ROUTES`` only) and through the task object
  (``apply_async``, which adds the task's own ``queue`` option first);
* ``BEAT_SCHEDULE`` — each entry's task, schedule (a crontab's five fields as written *and* as
  Celery expanded them, or an interval in seconds), ``args``, ``kwargs`` and ``options``;
* the app configuration the worker runs under (timezone, routes, acks, prefetch, visibility).

Runs from an empty working directory with no ``.env`` (the defaults are the contract) and no
network; it imports the worker, it never runs a task.
"""

from __future__ import annotations

import argparse
import datetime as dt
import inspect
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
DEFAULT_OUT = REPO / "rust/crates/baskfy-worker/tests/fixtures/python_task_registry.json"


def _default(value: Any) -> Any:
    if value is inspect.Parameter.empty:
        return {"required": True}
    return {"required": False, "default": value}


def _crontab(schedule: Any) -> dict[str, Any]:
    fields = ("minute", "hour", "day_of_week", "day_of_month", "month_of_year")
    return {
        "kind": "crontab",
        "written": {f: str(getattr(schedule, f"_orig_{f}")) for f in fields},
        "expanded": {f: sorted(getattr(schedule, f)) for f in fields},
    }


def dump() -> dict[str, Any]:
    from celery.schedules import crontab

    from baskfy_worker import celery_app as module
    from baskfy_worker.celery_app import BEAT_SCHEDULE, TASK_ROUTES, app

    import baskfy_worker.tasks.celery_tasks  # noqa: F401  (registers the shared tasks)
    import baskfy_worker.tasks.desk  # noqa: F401

    tasks: dict[str, Any] = {}
    for name in sorted(n for n in app.tasks if not n.startswith("celery.")):
        task = app.tasks[name]
        run = task.run
        params = []
        for p in inspect.signature(run).parameters.values():
            annotation = p.annotation if isinstance(p.annotation, str) else repr(p.annotation)
            params.append({"name": p.name, "annotation": annotation, **_default(p.default)})
        by_name = app.amqp.router.route({}, name, (), {})
        via_task = app.amqp.router.route(dict(task._get_exec_options()), name, (), {}, task)
        autoretry = getattr(task, "autoretry_for", ()) or ()
        tasks[name] = {
            "python": f"{run.__module__}.{getattr(run, '__qualname__', run.__name__)}",
            "params": params,
            # `bind=True` makes `run` a method of the task (it receives `self`); otherwise Celery
            # stores the function as a staticmethod.
            "bound": inspect.ismethod(run),
            "acks_late": bool(task.acks_late),
            "max_retries": task.max_retries,
            "default_retry_delay": task.default_retry_delay,
            "autoretry_for": [c.__name__ for c in autoretry],
            "retry_backoff": getattr(task, "retry_backoff", False),
            "retry_backoff_max": getattr(task, "retry_backoff_max", 600),
            "retry_jitter": getattr(task, "retry_jitter", True),
            "time_limit": task.time_limit,
            "soft_time_limit": task.soft_time_limit,
            "queue_option": getattr(task, "queue", None),
            "queue_by_name": by_name["queue"].name,
            "queue_via_task": via_task["queue"].name,
        }

    beat = []
    for entry, spec in BEAT_SCHEDULE.items():
        schedule = spec["schedule"]
        if isinstance(schedule, crontab):
            sched = _crontab(schedule)
        elif isinstance(schedule, dt.timedelta):
            sched = {"kind": "every", "seconds": schedule.total_seconds()}
        else:  # pragma: no cover - the table holds only the two kinds
            raise SystemExit(f"unknown schedule kind for {entry}: {schedule!r}")
        beat.append({
            "name": entry,
            "task": spec["task"],
            "schedule": sched,
            "args": list(spec.get("args", [])),
            "kwargs": dict(spec.get("kwargs", {})),
            "options": dict(spec.get("options", {})),
        })

    conf = app.conf
    return {
        "generated_by": "tools/parity/task_registry.py",
        "task_count": len(tasks),
        "tasks": tasks,
        "beat": beat,
        # A list of pairs: Celery tries the globs in order, and the first match wins.
        "task_routes": [[k, v["queue"]] for k, v in TASK_ROUTES.items()],
        "queues": list(module.QUEUES),
        "conf": {
            "timezone": conf.timezone,
            "enable_utc": conf.enable_utc,
            "task_default_queue": conf.task_default_queue,
            "task_acks_late": conf.task_acks_late,
            "task_reject_on_worker_lost": conf.task_reject_on_worker_lost,
            "worker_prefetch_multiplier": conf.worker_prefetch_multiplier,
            "visibility_timeout": conf.broker_transport_options.get("visibility_timeout"),
            "task_serializer": conf.task_serializer,
            "result_expires_seconds": conf.result_expires.total_seconds(),
            "task_track_started": conf.task_track_started,
            "task_default_retry_delay": conf.task_default_retry_delay,
            "task_annotations": conf.task_annotations,
        },
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory() as empty:
        deep = Path(empty) / "a" / "b"
        deep.mkdir(parents=True)
        os.chdir(deep)  # no .env can reach the settings
        data = dump()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, indent=1, sort_keys=False) + "\n")
    print(f"{data['task_count']} tasks, {len(data['beat'])} beat entries -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
