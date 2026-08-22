# Retiring the systemd timers — M19 §2

**Status: not started. The timers are running and must keep running.**

The desk's collection jobs now exist twice: as `momentum-daily.timer` on the Mumbai box, and as
Beat entries in the merged worker (`baskfy.desk.daily`, `baskfy.desk.autorun`). That duplication is
deliberate and temporary.

## Why running both is safe

`scripts/daily.py`'s own docstring: *"Every step is independently idempotent, so re-running changes
nothing. A step that fails never stops the rest."*

That is the entire basis for the overlap, so it is asserted rather than trusted —
`services/worker/tests/test_desk_tasks.py` runs the job twice and compares row counts across every
table the desk's `--check` reports on.

The Beat entry also fires at **exactly** the timer's hour, `Mon..Fri 18:30 IST`, and a test
compares the two. Not for tidiness: during the overlap, a Beat entry an hour later would be
collecting a *different* session's data while claiming to be the same job.

## The five-run rule

Retire the timers only after **five green Beat runs**, on five separate trading days.

A green run means: the task completed, `exit_code` was 0, and the desk's `--check` shows the
session's snapshot recorded. A run that exits 2 for want of a Kite token is **not** green — it is
the most common failure and the one the overlap exists to catch.

Five, not one, because the failure this protects against is not "Beat is broken". It is "Beat works
on the day someone is watching". Five separate days is a scheduler surviving five different sets of
circumstances: a late token, a holiday adjacent to a weekend, a worker restart, a slow broker.

Any non-green run resets the count to zero.

## The retirement itself — by hand, on the box

Not performed by this repository, and not by a deploy. Someone types these:

```bash
ssh <the box>
systemctl --user status momentum-daily.timer     # confirm what you are about to stop
systemctl --user disable --now momentum-daily.timer
systemctl --user list-timers                     # confirm it is gone
```

`momentum-backup.timer` is **not** part of this. It runs `scripts/backup`, has no Beat equivalent,
and is the thing that would let you recover from a mistake made here.

## Rolling back

```bash
systemctl --user enable --now momentum-daily.timer
```

One command, and the overlap is restored. Which is the point of retiring by disabling rather than
by deleting the unit file: the unit stays on disk, so the rollback needs no repository, no deploy,
and no network beyond the SSH session you are already in.

## What must be true before any of this starts

The Beat worker has to actually be running somewhere the box can reach — and today it is not.
`M19 §3`'s cutover, the observability in M20, and this retirement are three separate steps, in that
order. Retiring a timer in favour of a scheduler nobody is running is not a migration; it is an
outage with a plan attached.
