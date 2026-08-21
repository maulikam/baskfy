"""Build the ATM-straddle reference bands from your own history.

WHAT THE BRIEF ASKS FOR IS NOT OBTAINABLE, AND THE REASON MATTERS.
Section 9 says "run over your last 100 expiries". Kite cannot supply that. Its instrument
dump lists only LIVE contracts — checked 16 Aug 2026, zero of the eighteen NIFTY expiries
on file were in the past — and historical_data takes an instrument_token, not a symbol. The
moment a contract expires its token becomes unlookupable, so its price history is
unreachable from any Kite endpoint. There is no flag to change this and no fallback.

AND BACKWARD RECONSTRUCTION IS NOT MERELY LIMITED, IT IS UNSOUND. MEASURED:
A listed contract does have full history back to its listing date, so the first version of
this module read it. But the dte of a past session is defined against the contract that
GOVERNED that session, and for any date more than a week ago that contract has expired and
been dropped. Resolving against the live dump returns the wrong series: a mid-July session
resolved to the 18 Aug expiry and its three-week straddle was recorded as "dte 3+". The
run that exposed this produced 23 observations of which 22 were mislabelled, with a median
of 484 that is a three-week straddle, not a Wednesday one.

So an observation is only trustworthy inside a contract's FINAL WEEK, which is the only
window where the listed series really was the front series. Everything earlier is dropped.
That leaves almost nothing to reconstruct, and the readiness verdict says so.

THE ONLY SOUND PATH IS FORWARD COLLECTION: record the ATM straddle at entry time every
session from today. record_forward() below appends to the same schema, so the bands
improve as the record grows rather than needing to be rebuilt.

BANDS ARE BUILT ON THE OPENING PRINT, NOT THE CLOSE.
The entry gate compares ASP at roughly 09:15-09:30. A band built on closes is measured at
a different point in the decay curve than the thing it gates, and would sit low enough to
veto ordinary mornings.
"""
from __future__ import annotations

import datetime as dt
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

from .clock import resolve_expiry, trading_days_between

# Kite's historical endpoint is rate limited; this keeps a long calibration under it
# without needing a token bucket.
HISTORICAL_SLEEP_SECONDS = 0.34


@dataclass(frozen=True)
class Observation:
    session: dt.date
    expiry: dt.date
    dte: int
    bucket: str
    spot: float
    strike: float
    call: float
    put: float

    @property
    def straddle(self) -> float:
        return self.call + self.put

    @property
    def straddle_pct_of_spot(self) -> float:
        return self.straddle / self.spot * 100.0 if self.spot else 0.0

    def as_dict(self) -> dict:
        return {"session": self.session.isoformat(), "expiry": self.expiry.isoformat(),
                "dte": self.dte, "bucket": self.bucket, "spot": round(self.spot, 2),
                "strike": self.strike, "call": self.call, "put": self.put,
                "straddle": round(self.straddle, 2),
                "straddle_pct_of_spot": round(self.straddle_pct_of_spot, 4)}


def bucket_for(dte: int) -> str:
    return "3+" if dte >= 3 else str(dte)


def percentile(values: Sequence[float], p: float) -> float:
    """Linear-interpolated percentile. No numpy dependency for four numbers."""
    if not values:
        raise ValueError("no values")
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    idx = (len(s) - 1) * p
    lo, hi = int(idx), min(int(idx) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (idx - lo)


# =====================================================================================
# collection
# =====================================================================================
def _atm(spot: float, step: int) -> float:
    return round(spot / step) * step


def collect(kc, *, instruments: Sequence[Mapping[str, Any]], index_key: str,
            lookback_days: int, step: int, name: str,
            today: dt.date | None = None, series_days: int = 7,
            sleep: Callable[[float], None] = time.sleep,
            log: Callable[[str], None] = lambda _m: None) -> list[Observation]:
    """Reconstruct ATM straddle opens for every listed expiry, back to its listing date.

    One historical call per (expiry, strike, kind) rather than per date: each call returns
    the whole range, so the cost is the number of distinct ATM strikes the index visited,
    not the number of sessions.
    """
    today = today or dt.date.today()
    start = today - dt.timedelta(days=lookback_days)

    idx = [i for i in instruments if i.get("tradingsymbol") == index_key.split(":")[-1]
           and i.get("segment") == "INDICES"]
    if not idx:
        # NEVER fall back to a token. This defaulted to 256265, NIFTY 50, so a SENSEX run
        # whose BSE dump had not been passed would have reconstructed its bands against
        # NIFTY's spot history — computing ATM strikes near 24,000 for a chain that lives
        # near 77,000. Every strike lookup misses, the run reports NO_DATA, and nothing
        # says the wrong index was used.
        raise RuntimeError(
            f"no index instrument for {index_key!r} in the {len(instruments)} rows given. "
            "The cash-exchange dump for this underlying is missing; refusing to guess a "
            "token, because the wrong one reconstructs a plausible-looking band for the "
            "wrong index.")
    spot_token = int(idx[0]["instrument_token"])
    spot_rows = kc.historical_data(spot_token, start, today, "day")
    spot_by_day = {r["date"].date(): float(r["close"]) for r in spot_rows}
    if not spot_by_day:
        raise RuntimeError("no index history — the historical API returned nothing")
    log(f"index history: {len(spot_by_day)} sessions {min(spot_by_day)} -> {max(spot_by_day)}")

    opts = [i for i in instruments if i.get("name") == name
            and i.get("segment", "").endswith("-OPT") and i.get("expiry")]
    by_key: dict[tuple, dict] = {(i["expiry"], float(i["strike"]), i["instrument_type"]): i
                                 for i in opts}
    expiries = sorted({i["expiry"] for i in opts})
    log(f"{len(expiries)} listed expiries, {len(opts)} contracts")

    # Only the expiry that GOVERNS each session, never every listed expiry.
    #
    # Looping over all of them lumps a Wednesday weekly in with the June-2031 LEAPS: the
    # first run of this script produced a "dte 3+" band of 636-1171 with a maximum of
    # 9,873, because a five-year straddle also satisfies dte >= 3. The strategy only ever
    # sells the front weekly, so that is the only series a band may be built from.
    needed: dict[dt.date, dict[dt.date, float]] = defaultdict(dict)
    for day, spot in spot_by_day.items():
        if day > today:
            continue
        try:
            expiry = resolve_expiry(day, expiries)
        except Exception:                                    # noqa: BLE001
            continue
        needed[expiry][day] = _atm(spot, step)

    out: list[Observation] = []
    cache: dict[tuple, dict[dt.date, float]] = {}
    for expiry in expiries:
        days = needed.get(expiry) or {}
        strikes = sorted(set(days.values()))
        if not strikes:
            continue
        fetched = 0
        for strike in strikes:
            for kind in ("CE", "PE"):
                inst = by_key.get((expiry, strike, kind))
                if inst is None:
                    continue
                key = (expiry, strike, kind)
                if key in cache:
                    continue
                try:
                    rows = kc.historical_data(int(inst["instrument_token"]),
                                              start, today, "day")
                except Exception as exc:                      # noqa: BLE001
                    log(f"  {inst['tradingsymbol']}: {exc}")
                    cache[key] = {}
                    continue
                # OPEN, not close: the entry gate measures ASP at 09:15-09:30.
                cache[key] = {r["date"].date(): float(r["open"]) for r in rows
                              if float(r.get("open") or 0) > 0}
                fetched += 1
                sleep(HISTORICAL_SLEEP_SECONDS)
        made, dropped = 0, 0
        for day, strike in sorted(days.items()):
            ce = cache.get((expiry, strike, "CE"), {}).get(day)
            pe = cache.get((expiry, strike, "PE"), {}).get(day)
            if ce is None or pe is None or ce <= 0 or pe <= 0:
                continue
            dte = trading_days_between(day, expiry)
            if dte < 0:
                continue
            # Only a contract's final week is trustworthy. Before that the front series was
            # an earlier weekly that has since been delisted, so this contract's dte is not
            # the dte that session actually traded.
            if (expiry - day).days > series_days:
                dropped += 1
                continue
            out.append(Observation(session=day, expiry=expiry, dte=dte,
                                   bucket=bucket_for(dte), spot=spot_by_day[day],
                                   strike=strike, call=ce, put=pe))
            made += 1
        log(f"  {expiry}: {fetched} series fetched -> {made} observations"
            + (f" ({dropped} dropped: outside the contract's final week)" if dropped else ""))
    return out


# =====================================================================================
# bands
# =====================================================================================
def build_bands(obs: Iterable[Observation], *, low_pct: float = 0.25,
                high_pct: float = 0.75, min_samples: int = 8,
                iv_low_mult: float = 0.85, iv_high_mult: float = 1.30,
                max_dte: int | None = None) -> dict:
    """Percentile bands per dte bucket, with the veto rate they imply.

    The veto rate is the point of the report. A band is not "calibrated" because it was
    computed; it is calibrated when you can see what fraction of your own history it would
    have refused to trade. A band that vetoes 40% of sessions is a different strategy from
    the one described in the brief, and you should find that out here rather than by
    watching the bot skip nine days in a row.

    max_dte DISCARDS OBSERVATIONS THE STRATEGY WOULD NEVER HAVE TRADED, and it is not
    optional on a monthly series. bucket_for() puts every dte >= 3 in one "3+" bucket,
    which is exactly right for a weekly cycle whose furthest session is four days out. On
    BANKNIFTY, which has no weeklies, it pools a contract 30 days from expiry with one 5
    days out — measured 18 Aug 2026, those straddles were 1,836 and 780, a factor of 2.36.
    Since ~18 of 21 monthly sessions sit beyond the tradeable window, the far-dated
    observations DOMINATE the band that gates the final week.

    The direction of that failure is the bad one. The band lands about twofold too high, so
    its lower gate — low * iv_low_multiplier — sits ABOVE any genuine final-week straddle,
    and every real session is refused as "IV below band". On a representative record the
    band came out 1,240-1,840 against real straddles of 700-810: BANKNIFTY would never
    have traded a single session, and the reason logged would have looked like a market
    condition rather than a calibration bug.

    The raw forward record still keeps them: it cannot be recollected, and a band is a
    view over the data rather than the data itself.
    """
    grouped: dict[str, list[Observation]] = defaultdict(list)
    dropped = 0
    for o in obs:
        if max_dte is not None and o.dte > int(max_dte):
            dropped += 1
            continue
        grouped[o.bucket].append(o)

    bands: dict[str, dict] = {}
    for bucket, rows in sorted(grouped.items()):
        vals = [r.straddle for r in rows]
        low = percentile(vals, low_pct)
        high = percentile(vals, high_pct)
        veto_lo, veto_hi = low * iv_low_mult, high * iv_high_mult
        vetoed = sum(1 for v in vals if v < veto_lo or v > veto_hi)
        pct_vals = [r.straddle_pct_of_spot for r in rows]
        bands[bucket] = {
            "low": round(low, 2), "high": round(high, 2),
            "n": len(vals),
            "sufficient": len(vals) >= min_samples,
            "median": round(percentile(vals, 0.5), 2),
            "min": round(min(vals), 2), "max": round(max(vals), 2),
            # Kept so the band can be rescaled if the index level moves a lot; the gate
            # itself compares absolute points, which is why those are primary.
            "low_pct_of_spot": round(percentile(pct_vals, low_pct), 4),
            "high_pct_of_spot": round(percentile(pct_vals, high_pct), 4),
            "effective_veto_below": round(veto_lo, 2),
            "effective_veto_above": round(veto_hi, 2),
            "would_have_vetoed": vetoed,
            "would_have_vetoed_pct": round(vetoed / len(vals) * 100, 1),
            "sessions": sorted({r.session.isoformat() for r in rows})[:3],
            "expiries_covered": len({r.expiry for r in rows}),
            "dte_range": [min(r.dte for r in rows), max(r.dte for r in rows)],
        }
    return bands


def excluded_beyond(obs: Iterable[Observation], max_dte: int | None) -> int:
    """How many observations build_bands would discard as untradeable.

    Returned separately rather than mixed into the bands mapping: that mapping is handed
    straight to the rules engine as reference_band and iterated by the page, so a magic key
    inside it would become a phantom bucket in both.
    """
    if max_dte is None:
        return 0
    return sum(1 for o in obs if o.dte > int(max_dte))


def readiness(bands: Mapping[str, Mapping], *, tradeable_buckets=("3+", "2", "1"),
              excluded: int = 0) -> dict:
    """Whether these bands are fit to gate a paper run.

    Returns a verdict rather than raising, because a partly-calibrated table is still worth
    looking at — it just must not be silently treated as finished.
    """
    missing = [b for b in tradeable_buckets if b not in bands]
    thin = [b for b in tradeable_buckets
            if b in bands and not bands[b].get("sufficient")]
    tail = (f" {excluded} observation(s) sat beyond max_dte and were excluded: they price "
            "a contract this strategy would never have traded." if excluded else "")
    return {"ready": not missing and not thin,
            "missing_buckets": missing, "thin_buckets": thin,
            "excluded_beyond_max_dte": excluded,
            "note": (("every tradeable bucket has a sufficient sample"
                      if not missing and not thin else
                      "bands are incomplete — Kite cannot supply expired contracts, so "
                      "this is as far back as reconstruction reaches. Keep collecting "
                      "forward.") + tail)}


# =====================================================================================
# forward collection — the only sound path
# =====================================================================================
def record_forward(path: str, obs: Observation) -> None:
    """Append one session's ATM straddle to the forward record.

    JSONL and append-only for the same reason the order journal is: a calibration input
    that can be rewritten is not evidence. Duplicates for a session are tolerated and
    de-duplicated on read, because a re-run must never destroy a day.
    """
    import json
    import os
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps(obs.as_dict(), separators=(",", ":")) + "\n")


def load_forward(path: str) -> list[Observation]:
    """Read the forward record, keeping the FIRST entry per (session, expiry).

    First, not last: the first write is the one taken at entry time, which is what the gate
    compares against. A later re-run would record a different point in the decay curve.
    """
    import json
    import os
    if not os.path.exists(path):
        return []
    seen: dict[tuple, Observation] = {}
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            key = (d["session"], d["expiry"])
            if key in seen:
                continue
            seen[key] = Observation(
                session=dt.date.fromisoformat(d["session"]),
                expiry=dt.date.fromisoformat(d["expiry"]), dte=int(d["dte"]),
                bucket=str(d["bucket"]), spot=float(d["spot"]),
                strike=float(d["strike"]), call=float(d["call"]), put=float(d["put"]))
    return sorted(seen.values(), key=lambda o: (o.session, o.expiry))
