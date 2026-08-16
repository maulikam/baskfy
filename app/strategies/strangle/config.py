"""Config loading and the startup assertions that refuse to run on stale market facts."""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, Sequence

import yaml

from .clock import expiry_is_expected_weekday, resolve_expiry

DEFAULT_PATH = "config/strangle.yaml"


class StartupRefused(RuntimeError):
    """A verified market fact no longer holds. Never degrade to a warning."""


def load(path: str | Path = DEFAULT_PATH) -> dict:
    with open(path) as fh:
        return yaml.safe_load(fh)


def assert_market_facts(cfg: dict, instruments: Sequence[dict],
                        today: dt.date | None = None) -> dict:
    """Check lot size, strike step and expiry weekday against the live instrument dump.

    Lot sizes change — NIFTY went 75 -> 65 in Jan 2026 — and expiry days move, Thursday to
    Tuesday. A silent mismatch mis-sizes every position by about 15% and mis-keys every
    session parameter, and neither shows up as an error anywhere else.
    """
    ins = cfg["instrument"]
    name, step = ins["name"], int(ins["strike_step"])
    opts = [i for i in instruments
            if i.get("name") == name and i.get("segment") == f"{ins['exchange']}-OPT"]
    if not opts:
        raise StartupRefused(f"no {name} options in the instrument dump")

    lots = {int(i["lot_size"]) for i in opts}
    if lots != {int(ins["lot_size"])}:
        raise StartupRefused(
            f"lot_size is {sorted(lots)} in the dump, config says {ins['lot_size']}. "
            "Refusing to start: every position would be mis-sized.")

    strikes = sorted({float(i["strike"]) for i in opts})
    gaps = [round(b - a) for a, b in zip(strikes, strikes[1:]) if 0 < b - a <= step * 4]
    modal = max(set(gaps), key=gaps.count) if gaps else None
    if modal != step:
        raise StartupRefused(f"modal strike step is {modal}, config says {step}")

    today = today or dt.date.today()
    expiries = sorted({i["expiry"] for i in opts if i.get("expiry")})
    expiry = resolve_expiry(today, expiries)
    if not expiry_is_expected_weekday(expiry, ins["expiry_weekday"]):
        raise StartupRefused(
            f"nearest expiry {expiry} is a {expiry.strftime('%A')}, config expects "
            f"{ins['expiry_weekday']}. The expiry cycle has moved again.")

    return {"lot_size": int(ins["lot_size"]), "strike_step": step, "expiry": expiry,
            "expiry_weekday": expiry.strftime("%A"), "contracts": len(opts),
            "next_expiries": [e.isoformat() for e in expiries[:5]]}
