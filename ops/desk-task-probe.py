"""READ-ONLY probe: can this worker actually run the desk's scheduled collection?

Run with ``tools/deploy/box-python.sh worker ops/desk-task-probe.py``.

`baskfy.desk.daily` is on Beat at 18:30 IST Mon-Fri and it is the only scheduled job in the
product that captures the day's Kite fills (`app/analytics/tradebook.capture_live_trades`). It
locates the desk by walking up six parents from its own file and then `os.chdir`s there, so a
deployed image without the desk tree makes the entry unrunnable — silently, because a failed task
is logged and not retried.

This imports and inspects; it runs no task, so nothing is collected and nothing is written.

`gates/kite-sync.md` G9 is this script's output.
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
