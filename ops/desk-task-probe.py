"""READ-ONLY probe: did Beat stop scheduling desk collection?

Run with ``tools/deploy/box-python.sh worker ops/desk-task-probe.py``.

Until 13 Sep 2026 ``baskfy.desk.daily`` was on Beat at 18:30 IST Mon-Fri and could not
run: the worker image has no desk tree (NEEDS-MAULIK §33). Collection now runs in the
``desk-daily`` container. This probe asserts the worker half of that move: the Celery
names may still be registered as refusal stubs; Beat must not fire them.

This imports and inspects; it runs no task, so nothing is collected and nothing is written.
"""

from __future__ import annotations

from pathlib import Path

try:
    import baskfy_worker.tasks.desk as desk
except Exception as exc:  # noqa: BLE001 - a probe reports every failure shape as text
    print("desk task import FAILED:", type(exc).__name__, exc)
else:
    print("desk task module imported: yes")
    print("DESK_ROOT:", desk.DESK_ROOT)
    print("DESK_ROOT exists:", Path(desk.DESK_ROOT).is_dir())

from baskfy_worker.celery_app import app  # noqa: E402 - after the guarded import on purpose

names = sorted(name for name in app.tasks if name.startswith("baskfy."))
print("holdings tasks:", [n for n in names if "holdings" in n])
print("desk tasks:", [n for n in names if ".desk." in n])
print(
    "beat entries mentioning desk/holdings:",
    [k for k in app.conf.beat_schedule if "desk" in k or "hold" in k or "trade" in k],
)
print("total beat entries:", len(app.conf.beat_schedule))
print(
    "retired Beat keys absent:",
    "desk-daily-collection" not in app.conf.beat_schedule
    and "desk-autorun-safety-net" not in app.conf.beat_schedule,
)
