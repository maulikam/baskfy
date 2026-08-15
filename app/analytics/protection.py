"""Does every position actually have a working stop?

WHY THIS EXISTS
The rules say every buy gets a GTT stop in the same session, and the execution path does
place one. Nothing ever checked afterwards. That gap matters because the ways a stop
disappears are all silent:

- Positions bought before this system existed never had one.
- A GTT that fails to place leaves a filled position naked, and the failure is one line in
  a response nobody re-reads.
- GTTs expire (Zerodha ages them out), and can be cancelled or rejected later.
- A stop placed for the quantity held at the time protects only that much: buy more and
  the newer shares are uncovered.
- A position sold leaves an orphan trigger that can fire against a holding you no longer
  have.

"The code places stops" and "the book is protected" are different claims. Only this one is
checkable, so it compares live holdings against live GTTs and reports what is true now.

READ-ONLY. It never places, modifies or cancels a GTT. Arming seventeen stops is a real
decision with real money behind it, not something a status check should do on its own.
"""
from __future__ import annotations

from typing import Iterable, Mapping, Sequence

from .. import config as C
from ..core.guards import UntouchableInstrumentError, assert_tradeable

# Findings, worst first. The order is the order a human should read them in.
MISSING = "missing"          # no active stop at all
PARTIAL = "partial"          # a stop, but for fewer shares than are held
TOO_FAR = "too_far"          # further below than the configured band allows
TOO_CLOSE = "too_close"      # tight enough to be noise-triggered
ORPHAN = "orphan"            # a trigger with no matching holding
SEVERITY = {MISSING: 0, PARTIAL: 1, ORPHAN: 2, TOO_FAR: 3, TOO_CLOSE: 4}

ACTIVE = "active"


def _sym(gtt: Mapping) -> str:
    return str((gtt.get("condition") or {}).get("tradingsymbol") or "")


def _trigger(gtt: Mapping) -> float | None:
    vals = (gtt.get("condition") or {}).get("trigger_values") or []
    return float(vals[0]) if vals else None


def _qty(gtt: Mapping) -> int:
    """Total quantity the trigger would sell."""
    return sum(int(o.get("quantity") or 0) for o in (gtt.get("orders") or []))


def _protected(symbol: str) -> bool:
    """An untouchable instrument is not unprotected — it is deliberately never traded."""
    try:
        assert_tradeable(symbol)
        return True
    except UntouchableInstrumentError:
        return False


def review(holdings: Iterable[Mapping], gtts: Iterable[Mapping]) -> dict:
    """Compare what is held against what is actually triggered.

    holdings: [{symbol, quantity, last_price, ...}] as kite_client.holdings() returns.
    gtts:     kite_client raw GTT dicts.
    """
    held = {h["symbol"]: h for h in holdings}
    tradeable = {s: h for s, h in held.items() if _protected(s)}

    live: dict[str, list[dict]] = {}
    for g in gtts:
        if str(g.get("status") or "").lower() != ACTIVE:
            continue
        live.setdefault(_sym(g), []).append(g)

    findings: list[dict] = []

    for sym, h in tradeable.items():
        qty = int(h.get("quantity") or 0)
        px = float(h.get("last_price") or 0.0)
        rows = live.get(sym, [])
        if not rows:
            findings.append({"kind": MISSING, "symbol": sym, "qty": qty,
                             "value": round(qty * px),
                             "detail": "no active GTT stop"})
            continue

        covered = sum(_qty(g) for g in rows)
        triggers = [t for t in (_trigger(g) for g in rows) if t]
        best = max(triggers) if triggers else None      # the highest stop is the binding one

        if covered < qty:
            findings.append({
                "kind": PARTIAL, "symbol": sym, "qty": qty, "covered": covered,
                "value": round((qty - covered) * px),
                "detail": f"stop covers {covered} of {qty} shares"})

        if best and px > 0:
            drop = (1 - best / px)
            if drop > C.STOP_MAX + 1e-9:
                findings.append({
                    "kind": TOO_FAR, "symbol": sym, "qty": qty, "trigger": best,
                    "drop_pct": round(drop * 100, 2), "value": round(qty * px),
                    "detail": f"{drop * 100:.1f}% below, band allows "
                              f"{C.STOP_MIN * 100:.0f}-{C.STOP_MAX * 100:.0f}%"})
            elif drop < C.STOP_MIN - 1e-9:
                findings.append({
                    "kind": TOO_CLOSE, "symbol": sym, "qty": qty, "trigger": best,
                    "drop_pct": round(drop * 100, 2), "value": round(qty * px),
                    "detail": f"{drop * 100:.1f}% below, band allows "
                              f"{C.STOP_MIN * 100:.0f}-{C.STOP_MAX * 100:.0f}%"})

    for sym, rows in live.items():
        if sym not in held:
            findings.append({
                "kind": ORPHAN, "symbol": sym, "qty": sum(_qty(g) for g in rows),
                "value": 0,
                "detail": "active trigger with no matching holding"})

    findings.sort(key=lambda f: (SEVERITY.get(f["kind"], 9), -f.get("value", 0)))

    # Summed before rounding, not after: rounding each finding first made the unprotected
    # total exceed the tradeable total by a couple of rupees, which reads as a bug in the
    # very number the panel exists to make trustworthy.
    def exposure(sym, shares):
        return shares * float(held[sym].get("last_price") or 0.0)

    unprotected_raw = 0.0
    for f in findings:
        if f["kind"] == MISSING:
            unprotected_raw += exposure(f["symbol"], f["qty"])
        elif f["kind"] == PARTIAL:
            unprotected_raw += exposure(f["symbol"], f["qty"] - f["covered"])
    tradeable_raw = sum(int(h.get("quantity") or 0) * float(h.get("last_price") or 0.0)
                        for h in tradeable.values())
    unprotected_value = min(unprotected_raw, tradeable_raw)
    tradeable_value = tradeable_raw
    protected = [s for s in tradeable if not any(
        f["symbol"] == s and f["kind"] in (MISSING, PARTIAL) for f in findings)]

    return {
        "checked": len(tradeable),
        "excluded": sorted(set(held) - set(tradeable)),
        "active_gtts": sum(len(v) for v in live.values()),
        "protected": len(protected),
        "findings": findings,
        "counts": {k: sum(1 for f in findings if f["kind"] == k) for k in SEVERITY},
        "unprotected_value": round(unprotected_value),
        "tradeable_value": round(tradeable_value),
        # max(0, ...) so a rounding tail cannot print "-0.0%" coverage.
        "coverage_pct": (round(max(0.0, (tradeable_value - unprotected_value)
                                   / tradeable_value * 100), 1)
                         if tradeable_value else None),
        "healthy": not any(f["kind"] in (MISSING, PARTIAL) for f in findings),
    }


def from_kite(kite) -> dict:
    """Live review. Two API reads, no writes."""
    return review(kite.holdings(), kite.kc.get_gtts() or [])


def summary_line(rev: Mapping) -> str:
    if not rev.get("checked"):
        return "No tradeable positions to protect."
    if rev.get("healthy"):
        return (f"All {rev['checked']} tradeable positions carry a stop covering their "
                f"full quantity.")
    c = rev["counts"]
    bits = []
    if c.get(MISSING):
        bits.append(f"{c[MISSING]} with no stop at all")
    if c.get(PARTIAL):
        bits.append(f"{c[PARTIAL]} only partly covered")
    return (f"₹{rev['unprotected_value']:,.0f} of ₹{rev['tradeable_value']:,.0f} is "
            f"unprotected: {', '.join(bits)}.")


def _main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="check GTT stop coverage against holdings")
    ap.parse_args()

    from ..kite_client import Kite
    k = Kite()
    if not k.is_authed():
        raise SystemExit("Kite session expired — log in at / first")
    rev = from_kite(k)
    print(summary_line(rev))
    print(f"\nchecked {rev['checked']} tradeable positions · {rev['active_gtts']} active "
          f"GTTs · coverage {rev['coverage_pct']}%")
    if rev["excluded"]:
        print(f"excluded from the check (never traded): {', '.join(rev['excluded'])}")
    if rev["findings"]:
        print(f"\n{'issue':<10}{'symbol':<16}{'value':>13}  detail")
        for f in rev["findings"]:
            print(f"{f['kind']:<10}{f['symbol']:<16}{f['value']:>13,}  {f['detail']}")
    print("\nThis check never places or cancels a stop. Arming one is a decision with "
          "money behind it, not something a status check should do.")


if __name__ == "__main__":
    _main()
