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
EXCESS = "excess"            # triggers covering MORE shares than are held
# EXCESS outranks everything except a missing stop. An uncovered position loses money if
# the market falls; an over-covered one sells shares you do not own when it fires, which is
# short delivery and an auction penalty. On 18 Aug 2026 the book carried 10,383 shares of
# GTT against 9,478 held, because stops were armed from planned quantities before the
# orders had filled.
SEVERITY = {MISSING: 0, EXCESS: 1, PARTIAL: 2, ORPHAN: 3, TOO_FAR: 4, TOO_CLOSE: 5}

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
        elif covered > qty:
            findings.append({
                "kind": EXCESS, "symbol": sym, "qty": qty, "covered": covered,
                "gtt_ids": [g.get("id") for g in rows],
                "value": round((covered - qty) * px),
                "detail": f"stops cover {covered} shares but only {qty} are held; firing "
                          f"would sell {covered - qty} you do not own"})

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
                "gtt_ids": [g.get("id") for g in rows], "value": 0,
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
        # EXCESS and ORPHAN count as unhealthy, not just under-cover. On 18 Aug 2026 the
        # desk banner read "stops current — all 16 positions carry a stop covering their
        # full quantity" while 34 triggers were live across 16 symbols, every one of them
        # duplicated. Both of those findings can fire a sell for shares that are not there,
        # so a green light while either is outstanding is the most misleading state this
        # page can show.
        "healthy": not any(f["kind"] in (MISSING, PARTIAL, EXCESS, ORPHAN)
                           for f in findings),
    }


# A stop needs a volatility to be sized. Where the scan does not supply one, this is the
# fallback — the same figure app/rebalance.py uses, which lands mid-band at about 11%.
DEFAULT_VOL = 0.36


def build_stop_plan(holdings: Iterable[Mapping], gtts: Iterable[Mapping], *,
                    vol_by_symbol: Mapping[str, float] | None = None,
                    plan_id: str | None = None) -> dict:
    """Propose stops to arm, and triggers to cancel.

    MISSING and PARTIAL are proposed for. A stop that merely sits outside the band is left
    alone: replacing it means cancelling a live trigger, which is a different and riskier
    action than arming one where there is none.

    For a partially covered position the proposal is for the UNCOVERED shares only, so
    arming it cannot double up on quantity already protected.

    EXCESS AND ORPHAN PRODUCE CANCELS, because they cannot be fixed by adding. A stop
    covering more shares than are held sells what you do not own when it fires, and no
    additional trigger makes that better. For EXCESS the remedy is to cancel every trigger
    on that symbol and arm one for the quantity actually held; the cancel comes first so
    the two can never both be live.

    Pure. Takes prices and volatilities as data and returns a plan; it does not read the
    broker and it cannot place anything.
    """
    import uuid

    from ..scoring import stop_from_vol

    vols = dict(vol_by_symbol or {})
    rev = review(holdings, gtts)
    held = {h["symbol"]: h for h in holdings}

    cancels = []
    for f in rev["findings"]:
        if f["kind"] not in (EXCESS, ORPHAN):
            continue
        for gid in (f.get("gtt_ids") or []):
            cancels.append({"symbol": f["symbol"], "gtt_id": gid, "reason": f["kind"],
                            "detail": f["detail"]})

    rows = []
    for f in rev["findings"]:
        if f["kind"] not in (MISSING, PARTIAL, EXCESS):
            continue
        h = held.get(f["symbol"])
        if not h:
            continue
        px = float(h.get("last_price") or 0.0)
        # EXCESS re-arms the FULL held quantity, because every existing trigger on that
        # symbol is being cancelled. PARTIAL tops up only the uncovered shares.
        qty = (int(f["qty"]) if f["kind"] == EXCESS
               else int(f["qty"]) - int(f.get("covered") or 0))
        if qty <= 0 or px <= 0:
            continue
        vol = vols.get(f["symbol"])
        trigger = stop_from_vol(px, vol if vol else DEFAULT_VOL)
        rows.append({
            "symbol": f["symbol"], "qty": qty, "last_price": round(px, 2),
            "trigger": trigger,
            "drop_pct": round((1 - trigger / px) * 100, 2),
            "value": round(qty * px),
            "at_risk": round(qty * (px - trigger)),
            "vol": round(vol, 4) if vol else None,
            "vol_source": "scan" if vol else "default",
            "reason": f["kind"],
        })

    rows.sort(key=lambda r: -r["value"])
    return {
        "plan_id": plan_id or uuid.uuid4().hex[:12],
        "rows": rows,
        # Executed BEFORE the arms, so an over-covered symbol is never briefly protected
        # twice. Cancelling is the risk-reducing half of the operation.
        "cancels": cancels,
        "count": len(rows),
        "cancel_count": len(cancels),
        "value": sum(r["value"] for r in rows),
        "at_risk": sum(r["at_risk"] for r in rows),
        "using_default_vol": [r["symbol"] for r in rows if r["vol_source"] == "default"],
        "review": rev,
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
