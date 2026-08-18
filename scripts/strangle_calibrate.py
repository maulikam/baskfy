#!/usr/bin/env python
"""Build the ATM-straddle reference bands and, optionally, write them into the config.

READ-ONLY against the broker. No code path here places, modifies or cancels an order.

    python -m scripts.strangle_calibrate                 # report only
    python -m scripts.strangle_calibrate --write         # also update config/strangle.yaml
    python -m scripts.strangle_calibrate --lookback 120  # widen the reconstruction window

The bands cannot reach past the listing date of contracts that are still live, because
Kite drops expired contracts from the instrument dump and historical_data needs a token.
See app/strategies/strangle/calibrate.py for the full reasoning. The report always states
how much history it actually reached rather than implying the window it was asked for.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys

from app.kite_client import Kite
from app.strategies.strangle import calibrate as CAL
from app.strategies.strangle import config as SC
from app.strategies.strangle import instruments as INS


def main() -> int:
    ap = argparse.ArgumentParser(description="calibrate ATM straddle reference bands")
    ap.add_argument("--instrument", default=INS.DEFAULT, choices=INS.all_slugs())
    ap.add_argument("--config", default=None,
                    help="override the instrument's config file")
    ap.add_argument("--lookback", type=int, default=90,
                    help="calendar days to reconstruct (bounded by contract listing dates)")
    ap.add_argument("--low", type=float, default=0.25, help="lower percentile")
    ap.add_argument("--high", type=float, default=0.75, help="upper percentile")
    ap.add_argument("--min-samples", type=int, default=8)
    ap.add_argument("--write", action="store_true", help="write bands into the config")
    ap.add_argument("--json", action="store_true", help="machine-readable output only")
    args = ap.parse_args()

    und = INS.get(args.instrument)
    config_path = args.config or und.config
    cfg = SC.load(config_path)
    say = (lambda m: None) if args.json else (lambda m: print(m, file=sys.stderr))

    kite = Kite()
    if not kite.is_authed():
        print(json.dumps({"status": "AUTH_REQUIRED", "login_url": kite.login_url()},
                         indent=2))
        return 2

    ins = cfg["instrument"]
    # und.cash_exchange, not "NSE": the index itself is looked up here, and SENSEX quotes
    # on BSE. Hardcoding NSE would find no index row and reconstruct nothing, silently.
    instruments = (kite.kc.instruments(ins["exchange"])
                   + kite.kc.instruments(und.cash_exchange))
    say(f"reconstructing up to {args.lookback} days of ATM straddle opens...")

    obs = CAL.collect(kite.kc, instruments=instruments, index_key=ins["index_key"],
                      lookback_days=args.lookback, step=int(ins["strike_step"]),
                      name=ins["name"], log=say)
    if not obs:
        print(json.dumps({"status": "NO_DATA",
                          "note": "no ATM straddle could be reconstructed; every listed "
                                  "contract lacked opening prices in the window"}, indent=2))
        return 1

    bands = CAL.build_bands(obs, low_pct=args.low, high_pct=args.high,
                            min_samples=args.min_samples,
                            iv_low_mult=float(cfg["gates"]["iv_low_multiplier"]),
                            iv_high_mult=float(cfg["gates"]["iv_high_multiplier"]))
    ready = CAL.readiness(bands)
    span = (min(o.session for o in obs), max(o.session for o in obs))

    report = {
        "status": "OK",
        "instrument": und.slug,
        "observations": len(obs),
        "history_reached": {"from": span[0].isoformat(), "to": span[1].isoformat(),
                            "calendar_days": (span[1] - span[0]).days,
                            "expiries": len({o.expiry for o in obs})},
        "requested_lookback_days": args.lookback,
        "percentiles": {"low": args.low, "high": args.high},
        "bands": bands,
        "readiness": ready,
        "orders_submitted": 0,
    }
    print(json.dumps(report, indent=2, default=str))

    if not args.json:
        say("")
        for b, row in bands.items():
            flag = "" if row["sufficient"] else "   [THIN SAMPLE]"
            say(f"  dte {b:<3} n={row['n']:<4} band {row['low']:>7.1f}-{row['high']:<7.1f}"
                f"  vetoes {row['would_have_vetoed_pct']:>5.1f}% of its own history{flag}")
        say("")
        say(ready["note"])

    if args.write:
        if not ready["ready"]:
            say("refusing to write: " + ready["note"])
            say("re-run with a wider --lookback, or keep collecting forward.")
            return 1
        _write_bands(config_path, {b: {"low": r["low"], "high": r["high"], "n": r["n"]}
                                   for b, r in bands.items()})
        say(f"wrote {len(bands)} bands into {config_path}")
    return 0


def _write_bands(path: str, bands: dict) -> None:
    """Replace the reference_band block in place, preserving the rest of the file.

    A round-trip through yaml.safe_dump would strip every comment in the config, and those
    comments carry the verified sources for lot size, STT and the margin figure. Rewriting
    just the one block keeps them.
    """
    import yaml
    with open(path) as fh:
        lines = fh.readlines()
    out, skipping = [], False
    stamp = dt.date.today().isoformat()
    for line in lines:
        if line.startswith("reference_band:"):
            out.append(f"# Calibrated {stamp} by scripts/strangle_calibrate.py from live\n"
                       f"# contracts only — Kite cannot supply expired series.\n")
            out.append("reference_band:\n")
            out.append(yaml.safe_dump(bands, default_flow_style=False, sort_keys=True,
                                      indent=2).replace("\n", "\n  ").rstrip() + "\n")
            skipping = True
            continue
        if skipping:
            if line.strip() and not line.startswith((" ", "\t", "#")):
                skipping = False
            else:
                continue
        out.append(line)
    with open(path, "w") as fh:
        fh.writelines(out)


if __name__ == "__main__":
    raise SystemExit(main())
