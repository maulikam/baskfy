#!/bin/sh
# /opt/mb/run in the worker image: mb_task.py on the bundled interpreter. Args pass through.
exec /opt/mb/venv/bin/python -E -s /opt/mb/app/mb_task.py "$@"
