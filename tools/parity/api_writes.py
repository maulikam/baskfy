"""The write half of ``api_diff.py`` (``--writes``): scenarios on two fresh databases, side effects compared.

A GET is graded by its response. A write is graded by its response **and by what it did**: the
rows each table gained, changed and lost, what it enqueued in Redis, and the mail it sent. This
module runs one case file as one **scenario** — cases in order, each one sent to Python and then
to Rust — against two databases cloned for the run, and compares all four.

Databases (the bed only — ``127.0.0.1:55432``, container ``baskfy-rs-pg``)
----------------------------------------------------------------------------
``CREATE DATABASE … TEMPLATE baskfy`` needs the template to have no other session, and the bed
always has one: TimescaleDB's background-worker scheduler, plus whatever API servers other lanes
have open. So the clone is two-level (DECISIONS-RS R6.5):

1. **The lane template** ``rsw_tpl_<lane>`` is cloned from ``baskfy`` once, at a quiet moment (the
   harness retries until the only session left is the scheduler, which it ends — the launcher
   restarts it at once). In it every TimescaleDB job is unscheduled (no aggregate refresh or
   compression may run mid-scenario and show up as a diff, and no telemetry call leaves the box),
   and it is closed to connections, so no scheduler ever holds it. It is kept for the lane's
   next run; ``--drop-template`` removes it.
2. **Per run**, three databases are cloned from the lane template: ``rsw_<lane>_<run>_base`` (the
   state before the scenario), ``…_py`` and ``…_rs``. All three are dropped afterwards
   (``--keep-dbs`` keeps them for a look).

``baskfy`` itself is only ever a clone *source*: never connected to by a server in this mode,
never altered. Every name this module creates or drops must start with ``rsw_``; anything else is
refused before a statement is sent.

What is compared after the scenario
-----------------------------------
* **Tables.** Both servers are stopped (their backends exit, so PostgreSQL's per-table counters
  are flushed); every ``public`` table whose insert/update/delete counter moved on *either* side,
  plus any the case file names in ``"tables"``, is read from base, Python and Rust. Each side's
  change set — rows added, removed and changed, keyed by primary key — must be identical. A column
  in the case file's ``"db_mask"`` (``{"refresh_token": ["token_hash", "issued_at"]}``) is
  compared as ``<null>``, ``<unchanged>`` (equal to the base row) or ``<set>``/``<changed>``, so a
  timestamp the server stamps still has to be stamped by both. Every mask is printed.
* **Sequences.** ``last_value`` of every sequence, so an id one side burned and the other did not
  shows up even when no row survived.
* **Redis.** Each side runs on its own database index (``--redis-db-py``/``--redis-db-rs``, never
  0, flushed before the scenario). Afterwards every key is dumped by type; a key matching a
  ``"redis_presence_only"`` pattern (default: the rate-limit buckets, whose values hold a clock)
  is compared by presence; a Celery message has its task ids normalised.
* **Mail.** Both servers deliver SMTP to one sink this module runs on ``--smtp-port``. A message
  belongs to the case and side whose request was in flight when it arrived. Envelope, headers
  (From, To, Subject, Reply-To, MIME type) and each part's decoded text are compared after the
  case's ``"mail_mask"`` regular expressions.

Scenario mechanics
------------------
* ``"capture": {"name": "json:/pointer" | "cookie:NAME" | "header:NAME"}`` stores a value from a
  case's response, **per side** (Python's refresh token is not Rust's), and ``${name}`` in a later
  case's path, query, headers or body is replaced by that side's value.
* ``"mask"`` works as in GET mode for the body; ``"cookie:NAME"`` in it masks that cookie's value
  in ``Set-Cookie``, and ``"cookie-attr:expires"`` the value of that attribute (a deleted cookie's
  ``expires`` is the wall clock); every other attribute is always compared.
* ``"capture": {"code": "mail:with this code: (\\S+)"}`` takes group 1 of a regular expression
  from the text part of the mail the request sent (each side's own).
* File keys: ``"mode": "writes"`` (required), ``"env"`` (extra server environment), ``"db_mask"``,
  ``"tables"``, ``"redis_presence_only"``, and ``"seed_sql"`` — statements applied to all three
  clones (base included) before the servers start, so a fixture the bed lacks (a refresh token
  whose plaintext the scenario knows) is part of the baseline and never of the diff.
"""

from __future__ import annotations

import email
import email.policy
import json
import re
import secrets
import socket
import socketserver
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

CONTAINER = "baskfy-rs-pg"
BED_PORT = 55432
BED_USER = "baskfy"
SOURCE_DB = "baskfy"
PREFIX = "rsw_"
DEFAULT_PRESENCE_ONLY = [r"^ratelimit:", r"^rl:"]
CELERY_VOLATILE = ("id", "root_id", "parent_id", "correlation_id", "reply_to", "delivery_tag", "task_id")


# ---------------------------------------------------------------------------------------------
# Postgres (through the bed container's psql; the published port is checked first)
# ---------------------------------------------------------------------------------------------


def check_container() -> None:
    out = subprocess.run(
        ["docker", "port", CONTAINER, "5432/tcp"], capture_output=True, text=True, check=False
    ).stdout.strip()
    if f"127.0.0.1:{BED_PORT}" not in out.splitlines():
        raise SystemExit(f"{CONTAINER} does not publish 5432 on 127.0.0.1:{BED_PORT}; refusing")


def _guard_name(name: str) -> str:
    if name == SOURCE_DB or not re.fullmatch(rf"{PREFIX}[a-z0-9_]{{1,50}}", name):
        raise SystemExit(f"refusing to create, write or drop database {name!r} (only {PREFIX}* names)")
    return name


def psql(db: str, sql: str, *, check: bool = True) -> str:
    proc = subprocess.run(
        ["docker", "exec", "-i", CONTAINER, "psql", "-U", BED_USER, "-d", db, "-v", "ON_ERROR_STOP=1",
         "-At", "-q"],
        input=sql,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"psql on {db}: {proc.stderr.strip()}")
    return proc.stdout


def db_exists(name: str) -> bool:
    return psql("postgres", f"SELECT 1 FROM pg_database WHERE datname = '{name}'").strip() == "1"


def ensure_lane_template(lane: str, wait_seconds: float) -> str:
    """The lane's private, connection-closed copy of ``baskfy`` (see the module docs)."""
    tpl = _guard_name(f"{PREFIX}tpl_{lane}")
    if db_exists(tpl):
        return tpl
    deadline = time.monotonic() + wait_seconds
    while True:
        others = psql(
            "postgres",
            f"SELECT count(*) FROM pg_stat_activity WHERE datname = '{SOURCE_DB}' "
            "AND application_name NOT LIKE 'TimescaleDB%'",
        ).strip()
        if others == "0":
            done = subprocess.run(
                ["docker", "exec", "-i", CONTAINER, "psql", "-U", BED_USER, "-d", "postgres", "-At", "-q"],
                input=(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = "
                    f"'{SOURCE_DB}' AND application_name LIKE 'TimescaleDB%';\n"
                    f"CREATE DATABASE {tpl} TEMPLATE {SOURCE_DB} STRATEGY FILE_COPY;\n"
                ),
                capture_output=True,
                text=True,
                check=False,
            )
            if db_exists(tpl):
                break
            detail = done.stderr.strip().splitlines()[-1:] or [""]
            print(f"  clone of {SOURCE_DB} not yet possible ({detail[0]}); retrying")
        else:
            print(f"  {others} session(s) on {SOURCE_DB} (another lane's servers?); waiting to clone")
        if time.monotonic() > deadline:
            raise SystemExit(f"could not clone {SOURCE_DB} within {wait_seconds:.0f}s")
        time.sleep(3)
    # No TimescaleDB job may run mid-scenario (an aggregate refresh would show up as a diff, and
    # the telemetry job would call out); then close the template so no scheduler holds it.
    psql(tpl, "SELECT alter_job(job_id, scheduled => false) FROM timescaledb_information.jobs;")
    psql("postgres", f"ALTER DATABASE {tpl} WITH ALLOW_CONNECTIONS false;")
    psql("postgres", f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{tpl}';")
    return tpl


def clone(tpl: str, name: str) -> None:
    _guard_name(name)
    for _ in range(20):
        psql("postgres", f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{tpl}';",
             check=False)
        try:
            psql("postgres", f"CREATE DATABASE {name} TEMPLATE {tpl} STRATEGY FILE_COPY;")
            return
        except RuntimeError as exc:
            last = exc
            time.sleep(1)
    raise SystemExit(f"could not clone {tpl} into {name}: {last}")


def drop(name: str) -> None:
    _guard_name(name)
    psql("postgres", f"DROP DATABASE IF EXISTS {name} WITH (FORCE);", check=False)


def json_rows(db: str, sql: str) -> list[dict[str, Any]]:
    text = psql(db, f"SELECT coalesce(json_agg(q), '[]'::json) FROM ({sql}) q;")
    return json.loads(text, object_pairs_hook=dict) if text.strip() else []


def touched_tables(db: str) -> set[str]:
    rows = json_rows(
        db,
        "SELECT relname FROM pg_stat_user_tables WHERE schemaname = 'public' "
        "AND n_tup_ins + n_tup_upd + n_tup_del > 0",
    )
    return {r["relname"] for r in rows}


def primary_key(db: str, table: str) -> list[str]:
    rows = json_rows(
        db,
        "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid "
        f"AND a.attnum = ANY(i.indkey) WHERE i.indrelid = 'public.{table}'::regclass AND i.indisprimary "
        "ORDER BY array_position(i.indkey, a.attnum)",
    )
    return [r["attname"] for r in rows]


def table_rows(db: str, table: str, key: list[str]) -> dict[str, dict[str, Any]]:
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", table):
        raise SystemExit(f"odd table name {table!r}")
    order = ", ".join(key) if key else "1"
    rows = json_rows(db, f"SELECT * FROM public.{table} ORDER BY {order}")
    out: dict[str, dict[str, Any]] = {}
    for i, row in enumerate(rows):
        k = json.dumps([row.get(c) for c in key]) if key else json.dumps(row, sort_keys=True) + f"#{i}"
        out[k] = row
    return out


def sequences(db: str) -> dict[str, Any]:
    rows = json_rows(db, "SELECT schemaname || '.' || sequencename AS s, last_value FROM pg_sequences "
                         "WHERE schemaname = 'public'")
    return {r["s"]: r["last_value"] for r in rows}


def change_set(base: dict[str, dict[str, Any]], side: dict[str, dict[str, Any]], masked: set[str]) -> list[Any]:
    """Rows added, removed and changed relative to base, masked columns compared by kind."""

    def view(row: dict[str, Any], before: dict[str, Any] | None) -> list[Any]:
        out = []
        for col, value in row.items():
            if col in masked:
                if value is None:
                    value = "<null>"
                elif before is None:
                    value = "<set>"
                elif before.get(col) == value:
                    value = "<unchanged>"
                else:
                    value = "<changed>"
            out.append([col, value])
        return out

    changes: list[Any] = []
    for k in sorted(set(base) | set(side)):
        b, s = base.get(k), side.get(k)
        if b == s:
            continue
        if b is None:
            changes.append(["added", k, view(s, None)])
        elif s is None:
            changes.append(["removed", k, view(b, None)])
        else:
            changes.append(["changed", k, view(s, b)])
    return changes


# ---------------------------------------------------------------------------------------------
# Redis (RESP over a socket; stdlib only)
# ---------------------------------------------------------------------------------------------


class Resp:
    def __init__(self, host: str, port: int, db: int) -> None:
        if db == 0:
            raise SystemExit("refusing Redis database 0 for a write run (the GET lanes share it)")
        self.sock = socket.create_connection((host, port), timeout=10)
        self.buf = b""
        self.call("SELECT", str(db))

    def _line(self) -> bytes:
        while b"\r\n" not in self.buf:
            self.buf += self.sock.recv(65536)
        line, _, self.buf = self.buf.partition(b"\r\n")
        return line

    def _read(self) -> Any:
        line = self._line()
        kind, rest = line[:1], line[1:]
        if kind == b"+":
            return rest.decode()
        if kind == b"-":
            raise RuntimeError(rest.decode())
        if kind == b":":
            return int(rest)
        if kind == b"$":
            n = int(rest)
            if n < 0:
                return None
            while len(self.buf) < n + 2:
                self.buf += self.sock.recv(65536)
            data, self.buf = self.buf[:n], self.buf[n + 2 :]
            return data
        if kind == b"*":
            n = int(rest)
            return None if n < 0 else [self._read() for _ in range(n)]
        raise RuntimeError(f"bad RESP {line!r}")

    def call(self, *args: str | bytes) -> Any:
        parts = [b"*%d\r\n" % len(args)]
        for a in args:
            b = a.encode() if isinstance(a, str) else a
            parts.append(b"$%d\r\n%s\r\n" % (len(b), b))
        self.sock.sendall(b"".join(parts))
        return self._read()

    def close(self) -> None:
        self.sock.close()


def _normalise_celery(raw: bytes) -> Any:
    try:
        message = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return raw.decode("utf-8", "replace")
    if isinstance(message, dict) and isinstance(message.get("headers"), dict):
        for k in CELERY_VOLATILE:
            if k in message["headers"]:
                message["headers"][k] = "<task-id>"
        # Celery's `origin` is `gen<pid>@<host>`: the producer process's pid differs per side.
        if isinstance(message["headers"].get("origin"), str):
            message["headers"]["origin"] = re.sub(r"^gen\d+@.*$", "gen<pid>@<host>", message["headers"]["origin"])
        props = message.get("properties")
        if isinstance(props, dict):
            for k in CELERY_VOLATILE:
                if k in props:
                    props[k] = "<task-id>"
            if isinstance(props.get("delivery_info"), dict):
                props["delivery_info"].pop("delivery_tag", None)
        body = message.get("body")
        if isinstance(body, str) and message.get("properties", {}).get("body_encoding") == "base64":
            import base64

            try:
                message["body"] = json.loads(base64.b64decode(body))
            except ValueError:
                pass
    return message


def redis_dump(url: str, db: int, presence_only: list[str]) -> dict[str, Any]:
    parsed = urllib.parse.urlsplit(url)
    r = Resp(parsed.hostname or "127.0.0.1", parsed.port or 6379, db)
    out: dict[str, Any] = {}
    try:
        for key in r.call("KEYS", "*") or []:
            name = key.decode("utf-8", "replace")
            if any(re.search(p, name) for p in presence_only):
                out[name] = "<present>"
                continue
            kind = r.call("TYPE", key)
            ttl = r.call("TTL", key)
            if kind == "string":
                value: Any = _normalise_celery(r.call("GET", key) or b"")
            elif kind == "list":
                value = [_normalise_celery(v) for v in r.call("LRANGE", key, "0", "-1")]
            elif kind == "hash":
                flat = r.call("HGETALL", key)
                value = {flat[i].decode(): flat[i + 1].decode("utf-8", "replace") for i in range(0, len(flat), 2)}
            elif kind == "set":
                value = sorted(v.decode("utf-8", "replace") for v in r.call("SMEMBERS", key))
            elif kind == "zset":
                value = [v.decode("utf-8", "replace") for v in r.call("ZRANGE", key, "0", "-1", "WITHSCORES")]
            else:
                value = f"<{kind}>"
            out[name] = {"type": kind, "expires": ttl >= 0, "value": value}
    finally:
        r.close()
    return out


def redis_flush(url: str, db: int) -> None:
    parsed = urllib.parse.urlsplit(url)
    r = Resp(parsed.hostname or "127.0.0.1", parsed.port or 6379, db)
    try:
        r.call("FLUSHDB")
    finally:
        r.close()


# ---------------------------------------------------------------------------------------------
# The mail sink (a minimal SMTP server; both sides deliver to it)
# ---------------------------------------------------------------------------------------------


@dataclass
class Mail:
    sender: str
    rcpts: list[str]
    raw: bytes


@dataclass
class MailSink:
    port: int
    received: list[Mail] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    server: socketserver.ThreadingTCPServer | None = None

    def start(self) -> None:
        sink = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                w = self.wfile.write
                w(b"220 rs-parity-sink ESMTP\r\n")
                sender, rcpts = "", []
                while True:
                    line = self.rfile.readline()
                    if not line:
                        return
                    cmd = line.decode("utf-8", "replace").strip()
                    up = cmd.upper()
                    if up.startswith(("EHLO", "HELO")):
                        w(b"250-rs-parity-sink\r\n250-8BITMIME\r\n250 SMTPUTF8\r\n")
                    elif up.startswith("MAIL FROM:"):
                        sender, rcpts = cmd[10:].strip().split(" ")[0].strip("<>"), []
                        w(b"250 OK\r\n")
                    elif up.startswith("RCPT TO:"):
                        rcpts.append(cmd[8:].strip().strip("<>"))
                        w(b"250 OK\r\n")
                    elif up == "DATA":
                        w(b"354 End data with <CR><LF>.<CR><LF>\r\n")
                        data = b""
                        while True:
                            chunk = self.rfile.readline()
                            if not chunk or chunk in (b".\r\n", b".\n"):
                                break
                            data += chunk[1:] if chunk.startswith(b"..") else chunk
                        with sink.lock:
                            sink.received.append(Mail(sender, rcpts, data))
                        w(b"250 OK queued\r\n")
                    elif up == "QUIT":
                        w(b"221 Bye\r\n")
                        return
                    elif up in ("RSET", "NOOP"):
                        w(b"250 OK\r\n")
                    else:
                        w(b"502 Command not implemented\r\n")

        socketserver.ThreadingTCPServer.allow_reuse_address = True
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def mark(self) -> int:
        with self.lock:
            return len(self.received)

    def since(self, mark: int) -> list[Mail]:
        # A send completes before the response, but give a straggler a moment.
        time.sleep(0.05)
        with self.lock:
            return list(self.received[mark:])

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()


def mail_view(m: Mail, masks: list[str]) -> dict[str, Any]:
    def scrub(text: str) -> str:
        for pattern in masks:
            text = re.sub(pattern, "<masked>", text)
        return text

    msg = email.message_from_bytes(m.raw, policy=email.policy.default)
    parts = []
    for part in msg.walk():
        if part.is_multipart():
            parts.append({"multipart": part.get_content_type()})
            continue
        content = part.get_content()
        parts.append(
            {
                "type": part.get_content_type(),
                "charset": part.get_content_charset(),
                # Line endings are the transport's (7bit/QP parts arrive CRLF, base64 decodes to
                # what was encoded), so they are normalised; the text itself is compared.
                "content": scrub((content if isinstance(content, str) else repr(content)).replace("\r\n", "\n")),
            }
        )
    return {
        "envelope_from": m.sender,
        "envelope_to": m.rcpts,
        "from": str(msg.get("From")),
        "to": str(msg.get("To")),
        "subject": scrub(str(msg.get("Subject"))),
        "reply_to": str(msg.get("Reply-To")) if msg.get("Reply-To") else None,
        "mime_version": str(msg.get("MIME-Version")),
        "parts": parts,
    }


# ---------------------------------------------------------------------------------------------
# Captures and substitution
# ---------------------------------------------------------------------------------------------


def substitute(value: Any, variables: dict[str, str]) -> Any:
    if isinstance(value, str):
        def repl(m: re.Match[str]) -> str:
            name = m.group(1)
            if name not in variables:
                raise SystemExit(f"${{{name}}} was never captured")
            return variables[name]

        # `${name}`, or its URL-encoded spelling (a dict query is encoded before substitution).
        value = re.sub(r"%24%7B([A-Za-z0-9_]+)%7D", lambda m: urllib.parse.quote(repl(m), safe=""), value)
        return re.sub(r"\$\{([A-Za-z0-9_]+)\}", repl, value)
    if isinstance(value, list):
        return [substitute(v, variables) for v in value]
    if isinstance(value, dict):
        return {k: substitute(v, variables) for k, v in value.items()}
    return value


def set_cookies(raw_headers: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """Every ``Set-Cookie``, parsed: name, value, attributes in their order (keys lower-cased)."""
    out = []
    for k, v in raw_headers:
        if k.lower() != "set-cookie":
            continue
        first, *attrs = [p.strip() for p in v.split(";")]
        name, _, value = first.partition("=")
        parsed = []
        for a in attrs:
            ak, _, av = a.partition("=")
            parsed.append([ak.lower(), av] if "=" in a else [ak.lower()])
        out.append({"name": name, "value": value, "attrs": parsed})
    return out


def capture(
    spec: str, body: bytes, raw_headers: list[tuple[str, str]], mails: list[Mail] | None = None
) -> str:
    kind, _, where = spec.partition(":")
    if kind == "mail":
        # `mail:REGEX` — group 1 of the first match in the text of the mail this request sent.
        for m in mails or []:
            msg = email.message_from_bytes(m.raw, policy=email.policy.default)
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    found = re.search(where, part.get_content())
                    if found:
                        return found.group(1)
        raise SystemExit(f"capture {spec}: no mail from this request matches")
    if kind == "json":
        value: Any = json.loads(body)
        for part in [p for p in where.split("/") if p]:
            value = value[int(part)] if isinstance(value, list) else value[part]
        return value if isinstance(value, str) else json.dumps(value)
    if kind == "cookie":
        for c in set_cookies(raw_headers):
            if c["name"] == where:
                return str(c["value"])
        raise SystemExit(f"capture {spec}: no such Set-Cookie")
    if kind == "header":
        for k, v in raw_headers:
            if k.lower() == where.lower():
                return v
        raise SystemExit(f"capture {spec}: no such header")
    raise SystemExit(f"capture {spec}: unknown kind {kind!r}")


def new_run_id() -> str:
    return f"{int(time.time()) % 100000:05d}{secrets.token_hex(2)}"


def lane_ok(lane: str) -> str:
    if not re.fullmatch(r"[a-z0-9]{1,12}", lane):
        raise SystemExit("--lane must be 1-12 lower-case letters or digits")
    return lane


def env_for_side(base_env: dict[str, str], db_url: str, redis_url: str, redis_db: int, smtp_port: int,
                 extra: dict[str, str]) -> dict[str, str]:
    parsed = urllib.parse.urlsplit(redis_url)
    env = dict(base_env)
    env.update(
        {
            "BASKFY_DATABASE_URL": db_url,
            "BASKFY_REDIS_URL": f"redis://{parsed.hostname}:{parsed.port}/{redis_db}",
            "BASKFY_EMAIL_TRANSPORT": "smtp",
            "BASKFY_SMTP_HOST": "127.0.0.1",
            "BASKFY_SMTP_PORT": str(smtp_port),
            "BASKFY_SMTP_STARTTLS": "false",
        }
    )
    env.update(extra)
    return env


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) != 0


__all__ = [
    "MailSink",
    "capture",
    "change_set",
    "check_container",
    "clone",
    "drop",
    "ensure_lane_template",
    "env_for_side",
    "lane_ok",
    "mail_view",
    "new_run_id",
    "port_free",
    "primary_key",
    "redis_dump",
    "redis_flush",
    "sequences",
    "set_cookies",
    "substitute",
    "table_rows",
    "touched_tables",
]
