"""`providers doctor` — what can each provider currently serve? (Prompt 2 deliverable 6)

    "`providers doctor` runs without credentials and reports each provider as unavailable rather
     than crashing."

That constraint shapes the whole command: it makes no network calls, it catches everything an
adapter's own health check might throw, and it exits 0 when it has successfully *reported* a
problem. A non-zero exit is reserved for `--require-all`, which is what CI and the deploy check
would use.

Usage:
    python -m decile_providers.cli doctor
    python -m decile_providers.cli doctor --json
    python -m decile_providers.cli doctor --require-all
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from decile_providers.composite import CompositeProvider
from decile_providers.factory import build_provider_stack
from decile_providers.ports import Capability, ProviderHealth
from decile_providers.settings import ProviderSettings, get_provider_settings

EXIT_OK = 0
EXIT_UNAVAILABLE = 1


def doctor_report(stack: CompositeProvider) -> list[ProviderHealth]:
    """One health line per registered adapter. Never raises."""
    return list(stack.health_reports())


def render_text(reports: Sequence[ProviderHealth], stack: CompositeProvider) -> str:
    lines = ["Decile providers", "=" * 60]
    for report in reports:
        marker = "OK  " if report.available else "DOWN"
        lines.append(f"[{marker}] {report.name}")
        if report.detail:
            lines.append(f"        {report.detail}")
        serving = sorted(c.value for c in report.served_capabilities)
        offers = sorted(c.value for c in report.capabilities)
        lines.append(f"        serves now : {', '.join(serving) if serving else '(nothing)'}")
        lines.append(f"        can serve  : {', '.join(offers) if offers else '(nothing)'}")
        lines.append("")

    lines.append("Capability coverage")
    lines.append("-" * 60)
    for capability in Capability:
        holders = [r.name for r in reports if capability in r.served_capabilities]
        status = ", ".join(holders) if holders else "UNAVAILABLE"
        lines.append(f"  {capability.value:<20} {status}")

    unavailable = [r.name for r in reports if not r.available]
    lines.append("")
    if unavailable:
        lines.append(f"{len(unavailable)} provider(s) unavailable: {', '.join(unavailable)}")
    else:
        lines.append("All providers available.")
    del stack
    return "\n".join(lines)


def render_json(reports: Sequence[ProviderHealth]) -> str:
    payload = {
        "providers": [
            {
                "name": r.name,
                "available": r.available,
                "detail": r.detail,
                "serves_now": sorted(c.value for c in r.served_capabilities),
                "can_serve": sorted(c.value for c in r.capabilities),
            }
            for r in reports
        ],
        "coverage": {
            capability.value: sorted(r.name for r in reports if capability in r.served_capabilities)
            for capability in Capability
        },
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def run_doctor(
    settings: ProviderSettings | None = None,
    *,
    as_json: bool = False,
    require_all: bool = False,
    stack: CompositeProvider | None = None,
) -> tuple[int, str]:
    """Build the stack, probe it, and render. Returns ``(exit_code, output)``."""
    try:
        resolved = stack if stack is not None else build_provider_stack(settings)
    except Exception as exc:
        # Even a failure to construct the stack must be reported, not raised: this command is
        # what an operator runs precisely when something is misconfigured.
        message = f"could not build the provider stack: {type(exc).__name__}: {exc}"
        return EXIT_UNAVAILABLE, json.dumps({"error": message}) if as_json else message

    reports = doctor_report(resolved)
    output = render_json(reports) if as_json else render_text(reports, resolved)
    if require_all and any(not r.available for r in reports):
        return EXIT_UNAVAILABLE, output
    return EXIT_OK, output


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="providers", description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    doctor = subcommands.add_parser(
        "doctor", help="check credentials and report what each provider can serve"
    )
    doctor.add_argument("--json", action="store_true", help="machine-readable output")
    doctor.add_argument(
        "--require-all",
        action="store_true",
        help="exit non-zero if any provider is unavailable (for CI and deploy checks)",
    )

    args = parser.parse_args(argv)
    if args.command != "doctor":  # pragma: no cover - argparse rejects anything else
        parser.error(f"unknown command {args.command!r}")

    code, output = run_doctor(
        get_provider_settings(), as_json=bool(args.json), require_all=bool(args.require_all)
    )
    print(output)
    return code


if __name__ == "__main__":
    sys.exit(main())
