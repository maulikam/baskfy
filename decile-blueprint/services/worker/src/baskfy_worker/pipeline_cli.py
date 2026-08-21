"""Run the nightly pipeline for one date without a broker.

    python -m baskfy_worker.pipeline_cli --date 2026-08-18

Useful for a re-run after a gate failure, and for local development where standing up a worker and
Beat to process one date is more ceremony than the job deserves.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections.abc import Sequence

from baskfy_worker.db import run_in_session
from baskfy_worker.orchestrator import PipelineOutcome, run_nightly_pipeline
from baskfy_worker.providers import build_pipeline_dependencies
from baskfy_worker.steps import RunStatus


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m baskfy_worker.pipeline_cli", description=__doc__
    )
    parser.add_argument("--date", required=True, help="ISO trade date")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    trade_date = dt.date.fromisoformat(args.date)
    deps = build_pipeline_dependencies()
    outcome = run_in_session(lambda session: run_nightly_pipeline(session, trade_date, deps))

    if args.json:
        print(json.dumps(_payload(outcome), indent=2, sort_keys=True, default=str))
    else:
        print(f"{outcome.trade_date.isoformat()}: {outcome.status.value}")
        if outcome.data_version is not None:
            print(f"  published data_version {outcome.data_version}")
        if outcome.gate is not None:
            for result in outcome.gate.results:
                print(
                    f"  [{result.assertion}] {result.status.value:<7} "
                    f"{result.name}: {result.message}"
                )
        if outcome.error:
            print(f"  error: {outcome.error}")

    return 0 if outcome.status is RunStatus.SUCCEEDED else 1


def _payload(outcome: PipelineOutcome) -> dict[str, object]:
    return {
        "trade_date": outcome.trade_date.isoformat(),
        "status": outcome.status.value,
        "data_version": outcome.data_version,
        "failed_step": outcome.failed_step.value if outcome.failed_step else None,
        "error": outcome.error,
        "steps_completed": [s.value for s in outcome.steps_completed],
        "gate": outcome.gate.to_payload() if outcome.gate else None,
    }


if __name__ == "__main__":
    sys.exit(main())
