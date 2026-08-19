# Running the desk on a Mumbai box

Two problems, one move. Kite authorises order placement against an allowlist of IPs and
a residential connection does not hold one — `103.238.14.245` became `49.43.34.118`
inside a day, and the machine prefers a rotating IPv6 on top of that. Separately, the
09:20 and 18:30 jobs only run when the laptop is awake and logged in, which is why three
collection days were lost this week. A missed session is **not recoverable**:
`kc.margins()` has no history and `/trades` is flushed nightly.

A small always-on box in Mumbai fixes both, and is nearer the exchange than the laptop.

## Sizing

Measured, not estimated:

| | Resident |
|---|---|
| The desk, after touching every page including the regime backtest | 148 MB |
| Claude CLI, one working session | 467 MB |
| Ubuntu | ~150 MB |
| **Together** | **~765 MB** |

The desk is not the expensive half — the CLI is. A 1 GB instance runs the desk fine and
will OOM a real Claude session, so **4 GB** is the size to take: comfortable for both,
with room for a context spike. A 2 GB swapfile is created anyway, so a spike is a slow
moment rather than a killed process, and `momentum-web.service` is capped with
`MemoryMax` so the kernel takes the CLI first rather than the process holding live
plan_ids.

## Cost

| | per month | notes |
|---|---|---|
| AWS Lightsail, Mumbai, 1 GB | ~₹300 ($3.50) | static IP free while attached. Recommended. |
| Oracle Cloud Always Free | ₹0 | permanent reserved IP; ARM capacity in Mumbai is often unavailable, and free instances can be reclaimed |
| ISP static IP (business line) | ₹500–1,500 | no extra hop, slowest to provision |

## Backups

The whole irreplaceable set is about 10 MB, and losing it is not a storage problem — it
is the evidence the strategy is being judged on:

| | Rebuildable |
|---|---|
| `snapshots` | **No.** kc.margins() has no history; a lost EOD row is a lost day |
| `fills` | **No.** Zerodha flushes /trades nightly; only a partial Console export exists |
| `breadth_readings` | **No.** Needs a scan you no longer have |
| `rebalance_orders`, `regime_evaluations` | **No.** What was decided, and what happened |
| `index_series`, `benchmark` | Yes, re-fetchable from Kite |

`momentum-backup.timer` runs at 19:15 — after the 18:30 collection, so the archive holds
the EOD snapshot that job just wrote rather than yesterday's.

A live SQLite file **must not be copied**: a plain `cp` can catch a write in progress and
produce a file that opens cleanly and is missing the last transaction, which is the worst
kind of backup because it looks like one. `scripts/backup.py` uses sqlite's own backup
API, then reopens the copy, runs an integrity check and counts the rows that matter. A
run whose integrity check does not say `ok` exits non-zero, so a broken backup is loud in
`systemctl status` rather than silently green.

```
python -m scripts.backup            # take one
python -m scripts.backup --check    # what exists, and what the last one verified
```

Turn on **Lightsail automatic snapshots** as well — that covers the disk, the OS and the
venv; this covers the data and proves it can be opened. They answer different questions.

Pull a copy off the box periodically, because a snapshot in the same account as the
instance is not an offsite backup:

```
rsync -az desk@<ip>:kite-momentum-rebalancer/data/backups/ ./offsite-backups/
```

## Why SQLite, and not Postgres

Asked and answered with a measurement rather than a preference. The database is 5.7 MB
across roughly 42,000 rows, on one host, written by a handful of processes a few hundred
times a day. It already runs in **WAL** mode with a 30-second busy timeout and
`BEGIN IMMEDIATE` transactions, which is the configuration that makes concurrent writers
safe.

Tested: five processes — mimicking the web app, the daily job, the strangle runner, the
backup and the ops page — each committing 150 transactions. **750 writes in 0.2 seconds,
zero lock errors.** About 3,750 transactions a second against a workload that needs a few
hundred a day.

Postgres would add a service to run, patch, monitor and back up on the same box, a second
credential, a harder restore story, and migration risk on the one dataset that cannot be
rebuilt — in exchange for nothing measurable today.

Revisit it when any of these becomes true: a second application host, writers on a
different machine, a database into the many gigabytes, or a need for replication and
point-in-time recovery. None of them are on the path now.

## Security

This interface places real orders and the box holds credentials that do so. What is in
place, and why:

| Control | Against |
|---|---|
| Binds `127.0.0.1` only, firewall opens SSH alone | anything reaching port 8420 from outside |
| **Host allowlist** (`DESK_ALLOWED_HOSTS`) | DNS rebinding — an attacker's page resolving its own domain to 127.0.0.1 and reading `/performance/data` as same-origin |
| **Origin required on POST/PUT/DELETE** | CSRF — a form on any site posting to the desk while your tunnel is open. `/settings` and `/ops/run` take no secret, so they were reachable this way |
| **`DESK_PASSWORD`** (Basic auth) | other accounts, or a compromised process, on the same host. Loopback is not a boundary between users |
| API docs off (`DESK_DOCS=false`) | handing an attacker the route list, orders included |
| Credentials forced to `0600` at startup | `.env` and the token being world-readable under a default umask |
| SSH: keys only, no root, fail2ban, unattended upgrades | the one port that is open |

**Set `DESK_PASSWORD` on the box.** It is empty by default, which is right for a personal
laptop and wrong for anything hosted:

```
ssh desk@<ip>
python3 -c 'import secrets; print(secrets.token_urlsafe(24))'   # generate
nano kite-momentum-rebalancer/.env                              # DESK_PASSWORD=...
sudo systemctl restart momentum-web
```

Any username works at the browser prompt; only the password is checked, in constant time.

What is deliberately **not** claimed: this is a single-user desk behind a tunnel, not a
multi-tenant application. There is no user model, no audit of who did what, and no rate
limit on the login. Those are the right next controls if this ever has a second operator.

## Once

1. **Create the instance.** Lightsail → Mumbai (`ap-south-1`) → Ubuntu 24.04 → 1 GB.
   Attach a **static IP** (Networking → Create static IP → attach).
2. **Firewall: SSH only.** Delete every other rule in the Lightsail firewall. The web
   interface must never be reachable from the internet — it places real orders.
3. **Allowlist the static IP** in the Kite developer console. This is now a fixed value,
   so it stops expiring.
4. **Provision:**
   ```
   ssh ubuntu@<ip> 'bash -s' < deploy/bootstrap.sh
   ```
   Sets Asia/Kolkata, installs node, uv, the Claude CLI, creates the `desk` user, and
   locks the firewall to SSH.
5. **Copy the repo and its state:**
   ```
   ./deploy/sync.sh desk@<ip>
   ```
6. **Start it:**
   ```
   ssh ubuntu@<ip> 'cd /home/desk/kite-momentum-rebalancer && ./deploy/install-units.sh'
   ```

## Daily

```
ssh -L 8420:127.0.0.1:8420 desk@<ip>
```
Then open `http://127.0.0.1:8420` on the laptop exactly as before, and log in to Kite.

The tunnel is why the Kite redirect URL registered in the developer console still works:
the browser is on the laptop and still arrives at `127.0.0.1:8420/callback`. Nothing in
the Kite app configuration changes.

Logging in still triggers autorun, so whatever the day owes is collected at that moment.
The token expires nightly and Kite has no refresh, so this login is the one thing that
still needs a person — no host can remove it.

## Claude CLI on the box

Installed by `bootstrap.sh`. Sign in once, per user:

```
ssh desk@<ip>
cd kite-momentum-rebalancer
claude
```

It prints a URL; open it on the laptop, approve, paste the code back. After that the
session persists, and you can work on the desk from the box the same way as locally —
which is also where the market data and the live account now are.

Run it inside `tmux` for anything long, so a dropped SSH connection does not kill it:
`sudo apt-get install -y tmux && tmux new -s desk`.

## What runs, and when

| Unit | When | What |
|---|---|---|
| `momentum-web.service` | always | the interface, on loopback only |
| `momentum-daily.timer` | 18:30 Mon–Fri | index history, benchmarks, EOD snapshot, fills, stale orders |
| `strangle-collect@{nifty,banknifty,sensex}.timer` | 09:20 Mon–Fri | one ATM straddle observation each |

`momentum-daily` is `Persistent=true`: if the box was down at 18:30 it runs on boot,
because a late snapshot is still the day's snapshot.

`strangle-collect` is deliberately **not** persistent. A straddle recorded hours late is
a different point on the decay curve, and the reference bands are built on opening
prints — a mislabelled observation is worse than a missing one.

## Checking on it

```
systemctl status momentum-web
systemctl list-timers 'momentum-*' 'strangle-*'
journalctl -u momentum-daily --since today
tail -50 ~/logs/strangle-collect.log
```

Exit codes follow the same contract as on the laptop: `0` did its work, `2` nobody has
logged in to Kite yet, `1` anything else.

## After the cutover

The box keeps the record. `sync.sh` copies code every time but only fills in **missing**
state, so a later sync cannot overwrite the database the box has been writing. Use
`--force-state` only when you mean it.

Keep `FORCE_IPV4=true` in `.env`. The box will have a single stable v4 address, but the
setting costs nothing and removes any chance of the family flipping under you.
