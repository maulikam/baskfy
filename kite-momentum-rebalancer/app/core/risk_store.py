"""The account's risk state as one Postgres row per IST day, locked for every decision (LV3).

`baskfy_execution.risk.RiskManager` decides under ``store.lock()``: reload, decide, save. On the
desk that store is this class over ``public.risk_ledger`` (migration 0055): ``lock()`` opens a
transaction on the desk's connection and takes the day's row ``FOR UPDATE`` — inserting it first
if the day has none — so the desk, the swing monitor, ``twt-auto`` and the session supervisor
cannot each read the same headroom and each spend it. The row is the JSON payload the manager's
file path always wrote, so a reader of either shape reads the same thing.

On a sqlite desk (tests, a laptop) ``FOR UPDATE`` does not exist and ``BEGIN IMMEDIATE`` already
takes the write lock, so the statement is issued without it. The behaviour is the same: one
process at a time between lock and save.

Nothing here decides anything. It holds a row.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
from collections.abc import Callable, Iterator
from typing import Any, Final

IST: Final = dt.timezone(dt.timedelta(hours=5, minutes=30))


class PgRiskStateStore:
    """``RiskStateStore`` over ``risk_ledger`` through the desk's sqlite-shaped connection."""

    def __init__(
        self,
        connect: Callable[[], contextlib.AbstractContextManager[Any]],
        *,
        user_id: int,
        schema: str = "public",
        today: Callable[[], dt.date] | None = None,
    ) -> None:
        self._connect = connect
        self.user_id = int(user_id)
        self.schema = schema
        self._today = today or (lambda: dt.datetime.now(tz=IST).date())
        self._conn: Any = None
        self._depth = 0

    def t(self, table: str) -> str:
        return f"{self.schema}.{table}" if self.schema else table

    @staticmethod
    def _is_postgres(conn: Any) -> bool:  # noqa: ANN401 - a DB-API connection
        return conn.__class__.__module__.endswith("analytics.pg")

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        """Hold today's row exclusively until the block ends; re-entrant within a process."""
        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        with self._connect() as conn:
            self._conn = conn
            self._depth = 1
            postgres = self._is_postgres(conn)
            conn.execute("BEGIN" if postgres else "BEGIN IMMEDIATE")
            try:
                day = self._today()
                conn.execute(
                    f"INSERT INTO {self.t('risk_ledger')} (user_id, day, payload) "
                    "VALUES (?, ?, ?) ON CONFLICT (user_id, day) DO NOTHING",
                    (self.user_id, day, json.dumps({"day": day.isoformat()})),
                )
                conn.execute(
                    f"SELECT payload FROM {self.t('risk_ledger')} WHERE user_id = ? AND day = ?"
                    + (" FOR UPDATE" if postgres else ""),
                    (self.user_id, day),
                ).fetchone()
                yield
            except Exception:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")
            finally:
                self._conn = None
                self._depth = 0

    def _require_locked(self) -> Any:  # noqa: ANN401 - the held connection
        if self._conn is None:
            raise RuntimeError("risk_ledger is read and written only under lock()")
        return self._conn

    def load(self) -> dict[str, object] | None:
        conn = self._require_locked()
        row = conn.execute(
            f"SELECT payload FROM {self.t('risk_ledger')} WHERE user_id = ? AND day = ?",
            (self.user_id, self._today()),
        ).fetchone()
        if row is None:
            return None
        raw = row["payload"]
        payload = json.loads(raw) if isinstance(raw, (str, bytes)) else dict(raw)
        # A row inserted by lock() for a day nobody has decided on yet carries only the day.
        return payload if len(payload) > 1 else None

    def save(self, payload: dict[str, object]) -> None:
        conn = self._require_locked()
        conn.execute(
            f"INSERT INTO {self.t('risk_ledger')} (user_id, day, payload, updated_at) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT (user_id, day) DO UPDATE SET payload = EXCLUDED.payload, "
            "updated_at = CURRENT_TIMESTAMP",
            (self.user_id, self._today(), json.dumps(payload)),
        )
