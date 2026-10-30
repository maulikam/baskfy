"""Grade the TWT backtest (TW9), Python vs Rust — it has no Celery task, so ``task_diff`` cannot.

    python3 tools/parity/twt_backtest_diff.py [--case NAME ...] [--sides py,rs] --report /tmp/twt-bt.txt

(the repository's root; plain ``python3``.) Lane ``sleeve_backtests`` (wave 6).

``tools/twt/backtest.py`` (``make twt-backtest``) is the only caller of
``baskfy_worker.tasks.twt_backtest.run_twt_backtest``. This harness is ``task_diff.py`` with one
seam changed — what a side runs:

* ``py`` — this file's ``side`` subcommand: the script's ``_run`` body (one ``session.begin()``,
  ``run_twt_backtest`` with the case's start / end / sleeve / ETF flag, the report dict printed as
  ``json.dumps(..., default=str)``), with ``task_py``'s injected clock in the production modules;
* ``rs`` — ``target/debug/examples/twt_backtest`` (``rust/crates/baskfy-worker/examples``), the
  same body over ``baskfy_worker::tasks::sleeve_backtests::run_twt_backtest``.

Everything else — the clones, the database budget, the lock, the ports, and the comparison (the
payload with key order, every touched table by digest with the case's masks, sequences, Redis,
mail, upstream requests, archive files) — is ``task_diff``'s own code, imported, not copied.
Cases: ``tools/parity/task_cases/sleeve_backtests_twt.json`` (``args``: ``start``, ``end``,
``sleeve``, ``skip_etf_denominator`` — the script's four flags).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
CASES = HERE / "task_cases" / "sleeve_backtests_twt.json"


def side_main(raw_args: str, now: str) -> int:
    """The Python side: ``tools/twt/backtest.py``'s ``_run`` under the injected clock."""
    import asyncio  # noqa: PLC0415
    import datetime as dt  # noqa: PLC0415
    from decimal import Decimal  # noqa: PLC0415

    sys.path.insert(0, str(HERE))
    import task_py  # noqa: PLC0415

    task_py.install_clock(task_py.parse_now(now))
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: PLC0415

    from baskfy_api.settings import get_settings  # noqa: PLC0415
    from baskfy_core.twt.config import DEFAULT_TWT_CONFIG  # noqa: PLC0415
    from baskfy_worker.providers import sole_user_id  # noqa: PLC0415
    from baskfy_worker.tasks.twt_backtest import run_twt_backtest  # noqa: PLC0415

    args: dict[str, Any] = json.loads(raw_args)
    user_id = sole_user_id()
    if user_id is None:
        print(json.dumps({"error": "BASKFY_SOLE_USER_ID is not set, and the tw_ schema is keyed by user. "
                                   "Set it, or run `make seed` to create the development account."}))
        return 2

    async def run() -> dict[str, Any]:
        engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with maker() as session, session.begin():
                row = await run_twt_backtest(
                    session,
                    user_id=user_id,
                    start=dt.date.fromisoformat(args.get("start") or DEFAULT_TWT_CONFIG.backtest.start.isoformat()),
                    end=dt.date.fromisoformat(args["end"]) if args.get("end") else None,
                    sleeve_inr=Decimal(args.get("sleeve") or str(DEFAULT_TWT_CONFIG.backtest.initial_capital_inr)),
                    measure_etf_denominator=not args.get("skip_etf_denominator", False),
                )
                return {
                    "run_id": row.id,
                    "user_id": user_id,
                    "source": row.source,
                    "started_at": row.started_at.isoformat(),
                    "finished_at": row.finished_at.isoformat() if row.finished_at else None,
                    "params": row.params,
                    "stats": row.stats,
                    "drift": row.drift,
                }
        finally:
            await engine.dispose()

    try:
        report = asyncio.run(run())
    except Exception as exc:
        print(json.dumps({"raised": type(exc).__name__, "error": str(exc)}))
        return 1
    print(json.dumps(report, default=str))
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["side"]:
        return side_main(argv[argv.index("--args") + 1], argv[argv.index("--now") + 1])
    sys.path.insert(0, str(HERE))
    import nightly_diff as nd  # noqa: PLC0415
    import task_diff as td  # noqa: PLC0415

    keep = os.environ.get("TWT_BACKTEST_DIFF_KEEP")  # a directory: each side's full stdout kept

    def side_command(side: Any, case: dict[str, Any], bundle: Path, upstream: str, profile: str) -> list[str]:
        args = json.dumps(case.get("args", {}))
        if side.impl == "py":
            cmd = [str(td.BLUEPRINT / ".venv/bin/python"), str(Path(__file__).resolve()), "side",
                   "--args", args, "--now", case["now"]]
        else:
            cmd = [str(td.TARGET / profile / "examples" / "twt_backtest"), "--args", args, "--now", case["now"]]
        if not keep:
            return cmd
        out = Path(keep) / f"{case['name']}_{side.label}_{side.impl}.json"
        return ["/bin/bash", "-c", f'set -o pipefail; "$0" "$@" | tee {out}', *cmd]

    td.side_command = side_command
    rest = list(argv)
    if "--cases" not in rest:
        rest = ["--cases", str(CASES), *rest]
    if "--no-build" not in rest and "rs" in (rest[rest.index("--sides") + 1] if "--sides" in rest else "py,rs"):
        env = {**os.environ, "CARGO_TARGET_DIR": str(td.TARGET), "CARGO_BUILD_JOBS": "4"}
        profile = rest[rest.index("--profile") + 1] if "--profile" in rest else "debug"
        cmd = ["nice", "-n", "10", str(nd.CARGO), "build", "-p", "baskfy-worker", "--example", "twt_backtest"]
        if profile == "release":
            cmd.append("--release")
        subprocess.run(cmd, cwd=nd.REPO / "rust", env=env, check=True)
        rest.append("--no-build")
    return td.main(rest)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
