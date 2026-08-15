"""Read-only view model for /regime. Reads PRECOMPUTED values from SQLite only.

No Kite calls, no signal recomputation, no writes. The request path must never block on
the broker, and a status page must never be able to change the state it is reporting.

The page has to answer five questions within seconds — what regime, why, what exposure is
permitted, what is pending, is the data trustworthy — so the derivation lives here:
thresholds beside their current values, confirmation counters, plain-language narrative,
and the safe-state sentence. The template renders; it never calculates.

Fields with no data yet return None, never 0. A hollow number under a label reads as a
measurement and is worse than an honest absence.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any, Mapping, Sequence

from .. import config as C
from ..core.regime import AFFIRMATIVE_RISK_OFF, DISPLAY_REASONS, RegimeTier
from . import breadth as B
from . import db as DB
from . import regime_store as RS

EMPTY = {"available": False, "message": "No regime evaluation has been recorded yet."}

# tier -> (human name, semantic key, one-line meaning)
TIER_META: dict[str, tuple[str, str, str]] = {
    "R1": ("Risk-On", "on", "Full exposure permitted, full-size entries"),
    "R2": ("Caution", "caution", "Reduced exposure, half-size entries"),
    "R3": ("Risk-Off", "off", "Materially reduced exposure, no new entries"),
    "R4": ("Crash", "crash", "Minimum residual exposure, no new entries"),
}

# The whole ladder, so a tier can be read against the ones it is not.
TIER_LADDER = [
    {"tier": "R1", "name": "Risk-On", "key": "on", "buys": "Full size",
     "when": "Health strong on both the 50 and 200-DMA, breadth healthy, and the "
             "momentum sentinel above its 50-DMA.",
     "does": "Hold the full book. New positions enter at full weight."},
    {"tier": "R2", "name": "Caution", "key": "caution", "buys": "Half size",
     "when": "Nothing risk-off has fired, but the conditions for Risk-On are not all "
             "met — or the momentum sentinel has broken its 50-DMA.",
     "does": "Trim the weakest names back to the cap. New positions enter at half "
             "weight, or not at all while the sentinel veto is active."},
    {"tier": "R3", "name": "Risk-Off", "key": "off", "buys": "Blocked",
     "when": "Long-term health at or below its threshold, or breadth below its "
             "threshold, or the momentum sentinel below its 200-DMA.",
     "does": "Sell down until actual exposure reaches the cap. No new positions."},
    {"tier": "R4", "name": "Crash", "key": "crash", "buys": "Blocked",
     "when": "Most of the book in full bearish alignment with very weak breadth, or "
             "the sentinel below its 200-DMA with weak long-term health and breadth.",
     "does": "Reduce to a minimum residual, keeping only the names the residual "
             "policy names. No new positions."},
]

# Plain-language meaning of the three independent switches in the header.
def _mode_explainer(regime_enabled: bool, mode: str, dry_run: bool) -> list[dict]:
    return [
        {"label": "Feature",
         "value": "Enabled" if regime_enabled else "Disabled",
         "state": "on" if regime_enabled else "off",
         "means": ("The regime overlay is switched on and may influence the rebalancer."
                   if regime_enabled else
                   "The regime overlay is switched off entirely. It calculates and "
                   "displays a view, but changes nothing about how the portfolio is run.")},
        {"label": "Mode", "value": (mode or "observe").title(),
         "state": {"observe": "off", "propose": "caution", "enforce": "on"}.get(mode, "off"),
         "means": {"observe": "Watch only. Decisions are calculated and shown, but no "
                              "executable plan is created.",
                   "propose": "A plan is built and shown for your review. Nothing runs "
                              "until you approve it.",
                   "enforce": "An approved plan may be sent to the broker through the "
                              "normal order path."}.get(mode, "Watch only.")},
        {"label": "Orders", "value": "Dry run" if dry_run else "Live",
         "state": "off" if dry_run else "on",
         "means": ("Simulation. No order can reach the broker, no policy tier can be "
                   "committed, and no rebalance can be marked executed."
                   if dry_run else
                   "Real orders can be placed when a plan is approved in enforce mode.")},
    ]
NEW_BUY_LABEL = {"full": "Full size", "half": "Half size", "blocked": "Blocked"}
FORCED_LABEL = {"none": "None", "trim_to_cap": "Trim to cap",
                "reduce_to_cap": "Reduce to cap", "crash_reduce": "Crash reduction",
                "manual_review": "Manual review"}
BREADTH_BANDS = [(0.0, 30.0, "Crash breadth", "crash"), (30.0, 40.0, "Risk-off", "off"),
                 (40.0, 55.0, "Caution", "caution"), (55.0, 101.0, "Recovery", "on")]


def _row_to_dict(row) -> dict:
    return dict(row) if row is not None else {}


def _pct(v, digits=1):
    return None if v is None else f"{v:.{digits}f}%"


# =====================================================================================
# sources
# =====================================================================================
def latest_evaluation(conn) -> tuple[dict, bool]:
    """The canonical committed row if one exists, else the most recent preview."""
    row = RS.latest_committed(conn)
    if row is not None:
        return _row_to_dict(row), True
    row = conn.execute(
        "SELECT * FROM regime_evaluations ORDER BY created_at DESC, id DESC LIMIT 1"
    ).fetchone()
    return _row_to_dict(row), False


def index_status(conn, names: list[str], as_of: dt.date | None) -> list[dict]:
    out = []
    for name in names:
        r = conn.execute(
            "SELECT MAX(date) d, COUNT(*) n FROM index_series "
            "WHERE index_name=? AND is_final=1", (name,)).fetchone()
        last = dt.date.fromisoformat(r["d"]) if r and r["d"] else None
        age = (as_of - last).days if (last and as_of) else None
        out.append({"index_name": name,
                    "last_final_session": last.isoformat() if last else None,
                    "sessions": r["n"] if r else 0, "age_days": age,
                    "stale": bool(age is not None and age > C.REGIME_INDEX_STALE_DAYS),
                    "has_data": bool(last)})
    return out


# =====================================================================================
# derivation
# =====================================================================================
def _confirmation(ma: dict, cfg) -> str:
    """'3/3 below' — how much of the confirmation requirement currently holds."""
    state = ma.get("state", "unknown")
    if state == "unknown":
        return f"0/{cfg.confirm_days} unconfirmed"
    n = min(int(ma.get("confirming_closes") or 0), cfg.confirm_days)
    return f"{n}/{cfg.confirm_days} {state}"


SPARK_SESSIONS = 130            # about six months of trading
SPARK_W, SPARK_H = 168, 34


def _sparkline(closes: list[float], ma_len: int) -> dict | None:
    """Close against the moving average that decides this card, over ~6 months.

    The card already states every MA distance as a number. A sparkline of price alone
    would only repeat the close, so the reference line is the MA that actually drives the
    decision for this index — the 200-DMA for the structural indices, whose H200 sets the
    tier, and the 50-DMA for the sentinel, whose crossing is the new-buy veto. That makes
    the shape answer a question the numbers do not: is this rolling over or recovering.

    Needs ma_len sessions of history BEFORE the visible window, so the average is real at
    the first drawn point rather than creeping up from a short window.
    """
    if len(closes) < ma_len + 2:
        return None
    ma = [sum(closes[i - ma_len + 1:i + 1]) / ma_len if i >= ma_len - 1 else None
          for i in range(len(closes))]
    start = max(ma_len - 1, len(closes) - SPARK_SESSIONS)
    px_v, ma_v = closes[start:], ma[start:]
    if len(px_v) < 2:
        return None

    pts = [v for v in px_v + [m for m in ma_v if m is not None]]
    lo, hi = min(pts), max(pts)
    span = (hi - lo) or (hi or 1.0) * 0.01
    n = len(px_v) - 1

    def x(i): return i / n * (SPARK_W - 2) + 1
    def y(v): return SPARK_H - 2 - (v - lo) / span * (SPARK_H - 4)

    def path(vals):
        out, pen = [], "M"
        for i, v in enumerate(vals):
            if v is None:
                pen = "M"
                continue
            out.append(f"{pen}{x(i):.1f},{y(v):.1f}")
            pen = "L"
        return " ".join(out)

    return {"w": SPARK_W, "h": SPARK_H, "ma_len": ma_len,
            "price": path(px_v), "ma": path(ma_v),
            "last_x": round(x(n), 1), "last_y": round(y(px_v[-1]), 1),
            "above": px_v[-1] >= (ma_v[-1] if ma_v[-1] is not None else px_v[-1]),
            "sessions": len(px_v)}


def _spark_series(conn, cfg, order: Sequence[str]) -> dict:
    """Closes per index, deep enough to compute a real 200-DMA at the window start."""
    from . import index_cache as IC

    out: dict = {}
    try:
        repo = IC.IndexRepository(conn)
        for name in order:
            need = SPARK_SESSIONS + 200 + 5
            candles = repo.load(name)[-need:]
            out[name] = [c.close for c in candles if c.close]
    except Exception:
        return {}
    return out


def _index_cards(diagnostics: dict, cfg, status_rows: list[dict],
                 spark_series: Mapping[str, list[float]] | None = None) -> list[dict]:
    by_name = {r["index_name"]: r for r in status_rows}
    order = list(cfg.structural_indices.values()) + [cfg.momentum_sentinel]
    cards = []
    for name in order:
        d = diagnostics.get(name)
        if not d:
            continue
        sigs = d.get("signals", {})
        # Both forms: the formatted string is what the page shows ("3/3 above"), the raw
        # count stays for JSON consumers that want the number rather than the sentence.
        mas = [{"length": n, "ma": (sigs.get(str(n)) or {}).get("ma"),
                "distance_pct": (sigs.get(str(n)) or {}).get("distance_pct"),
                "state": (sigs.get(str(n)) or {}).get("state", "unknown"),
                "confirming_closes": (sigs.get(str(n)) or {}).get("confirming_closes", 0),
                "confirmation": _confirmation(sigs.get(str(n)) or {}, cfg)}
               for n in (20, 50, 200)]
        st = by_name.get(name, {})
        is_sentinel = name == cfg.momentum_sentinel
        # The sentinel is judged on its 50-DMA crossing (the new-buy veto); the structural
        # indices on their 200-DMA (H200, which sets the tier).
        spark = _sparkline((spark_series or {}).get(name, []), 50 if is_sentinel else 200)
        cards.append({"index_name": name, "is_sentinel": is_sentinel,
                      "spark": spark,
                      "close": d.get("close"), "mas": mas,
                      "bearish_stack": bool(d.get("bearish_stack")),
                      "data_date": d.get("as_of_date"),
                      "stale": bool(d.get("is_stale") or st.get("stale")),
                      "sessions": st.get("sessions")})
    return cards


def _gates(health, breadth_pct, breadth_usable, sentinel_card, stack_weight, cfg,
           codes) -> list[dict]:
    """Every decisive gate with its current value, threshold and status.

    Showing only the rules that fired explains what happened but not why nothing else did.
    Showing all of them, each number beside its threshold, is what makes the tier auditable.
    """
    def num(x, fmt="{:.2f}"):
        return None if x is None else fmt.format(x)

    def state(triggered, evaluable=True, met_label="Triggered", clear="Clear"):
        return clear if not triggered else met_label if evaluable else "Not evaluable"

    h20, h50, h200 = health.get("20"), health.get("50"), health.get("200")
    mas = (sentinel_card or {}).get("mas", [])
    s50 = next((m for m in mas if m["length"] == 50), {})
    s200 = next((m for m in mas if m["length"] == 200), {})
    buf = cfg.buffer_bps / 100.0

    rows = [
        {"code": "R3_H200_WEAK", "label": "Long-term structural health (H200)",
         "current": num(h200), "threshold": f"≤ {cfg.r3_h200_max:.2f}",
         "status": ("Not evaluable" if h200 is None else
                    "Triggered" if h200 <= cfg.r3_h200_max else "Clear"),
         "note": "Weighted share of the book above its 200-DMA"},
        {"code": "R3_BREADTH_WEAK", "label": "Breadth",
         "current": _pct(breadth_pct) if breadth_usable else None,
         "threshold": f"< {cfg.r3_breadth_max:.0f}%",
         "status": ("Not evaluable" if not breadth_usable or breadth_pct is None else
                    "Triggered" if breadth_pct < cfg.r3_breadth_max else "Clear"),
         "note": "Universe above its 20-DMA"},
        {"code": "R3_SENTINEL_BELOW_200DMA", "label": "Momentum 50 vs 200-DMA",
         "current": num(s200.get("distance_pct"), "{:+.2f}%"),
         "threshold": f"below −{buf:.2f}%",
         "status": ("Not evaluable" if not s200 else
                    "Triggered" if s200.get("state") == "below" else "Clear"),
         "note": s200.get("confirmation", "")},
        {"code": "SENTINEL_BELOW_50DMA_VETO", "label": "Momentum 50 vs 50-DMA",
         "current": num(s50.get("distance_pct"), "{:+.2f}%"),
         "threshold": f"below −{buf:.2f}%",
         "status": ("Not evaluable" if not s50 else
                    "Triggered" if s50.get("state") == "below" else "Clear"),
         "note": s50.get("confirmation", "")},
        {"code": "R4_BEARISH_STACK_AND_WEAK_BREADTH", "label": "Bearish MA stack weight",
         "current": num(stack_weight),
         "threshold": f"≥ {cfg.r4_bearish_stack_min:.2f} with breadth < "
                      f"{cfg.r4_breadth_max:.0f}%",
         "status": ("Not evaluable" if stack_weight is None else
                    "Triggered" if (stack_weight >= cfg.r4_bearish_stack_min
                                    and breadth_usable and breadth_pct is not None
                                    and breadth_pct < cfg.r4_breadth_max) else "Clear"),
         "note": "Share of the book in full bearish alignment"},
        {"code": "R1_ALL_CONDITIONS_MET", "label": "Risk-On health requirement",
         "current": f"H50 {num(h50) or '—'} · H200 {num(h200) or '—'}",
         "threshold": f"≥ {cfg.r1_h50_min:.2f} and ≥ {cfg.r1_h200_min:.2f}",
         "status": ("Met" if (h50 is not None and h200 is not None
                              and h50 >= cfg.r1_h50_min and h200 >= cfg.r1_h200_min)
                    else "Not met"),
         "note": "Both must hold for Risk-On"},
        {"code": "R1_BREADTH", "label": "Risk-On breadth requirement",
         "current": _pct(breadth_pct) if breadth_usable else None,
         "threshold": (f"≥ {cfg.r1_breadth_min:.0f}%" if cfg.breadth_required
                       else "not required"),
         "status": ("Not required" if not cfg.breadth_required else
                    "Not evaluable" if not breadth_usable or breadth_pct is None else
                    "Met" if breadth_pct >= cfg.r1_breadth_min else "Not met"),
         "note": "Breadth gates Risk-On when it is part of the model"},
    ]
    for r in rows:
        r["fired"] = r["code"] in codes
    return rows


def _narrative(cards, health, breadth_pct, breadth_usable, new_buys, cfg, codes
               ) -> list[str]:
    """Plain-language sentences, most decisive first."""
    out: list[str] = []
    for c in cards:
        if c["is_sentinel"]:
            continue
        below = {m["length"] for m in c["mas"] if m["state"] == "below"}
        if 200 in below:
            out.append(f"{c['index_name']} is confirmed below its 200-DMA.")
        elif 50 in below:
            out.append(f"{c['index_name']} is confirmed below its 50-DMA.")
    h200 = health.get("200")
    if h200 is not None and h200 <= cfg.r3_h200_max:
        out.append(f"Weighted 200-DMA health is {h200:.2f}, at or below the "
                   f"{cfg.r3_h200_max:.2f} risk-off threshold.")
    if breadth_usable and breadth_pct is not None and breadth_pct < cfg.r3_breadth_max:
        out.append(f"Breadth is {breadth_pct:.0f}%, below the {cfg.r3_breadth_max:.0f}% "
                   f"risk-off threshold.")
    elif "BREADTH_NOT_USED" in codes:
        out.append("Breadth is not part of this configuration, so it neither forces "
                   "risk-off nor gates Risk-On.")
    elif not breadth_usable:
        out.append("There is no usable breadth reading, so breadth can neither force "
                   "selling nor permit Risk-On.")
    sent = next((c for c in cards if c["is_sentinel"]), None)
    if sent:
        s50 = next((m for m in sent["mas"] if m["length"] == 50), {})
        if s50.get("state") == "below":
            out.append(f"{sent['index_name']} remains below its 50-DMA "
                       f"({s50.get('distance_pct') or 0:+.1f}%).")
    out.append({"blocked": "New purchases are therefore blocked.",
                "half": "New purchases are permitted at half size.",
                "full": "Full-size new purchases are permitted."}.get(new_buys, ""))
    return [s for s in out if s]


def _banners(ev, v) -> list[dict]:
    """Strong banners, most severe first. Each names the condition AND its consequence."""
    out = []
    if v["data_stale"]:
        out.append({"level": "warn", "title": "Index data is stale",
                    "detail": f"Policy tier {v['policy_tier']} retained, new purchases "
                              f"blocked, and no forced regime sales generated."})
    if not v["breadth_usable"]:
        out.append({"level": "warn", "title": "Breadth coverage insufficient",
                    "detail": "Breadth cannot trigger forced selling and cannot "
                              "permit Risk-On."})
    if v["tax_data_incomplete"]:
        out.append({"level": "warn", "title": "Tax lot data incomplete",
                    "detail": "Affected sales need manual review. No sale is assumed "
                              "tax-safe."})
    if v["manual_action_required"]:
        out.append({"level": "warn", "title": "Manual action required",
                    "detail": "The exposure target cannot be reached automatically."})
    if v["dry_run"]:
        out.append({"level": "hold", "title": "DRY_RUN is on",
                    "detail": "No order can be submitted, no policy tier committed, and "
                              "no rebalance marked executed."})
    if v["mode"] == "observe":
        out.append({"level": "hold", "title": "Observe mode",
                    "detail": "Decisions are calculated and displayed only. No executable "
                              "regime plan is produced."})
    return out


def _safe_state(v: dict) -> str | None:
    """One sentence naming the protective behaviour currently in force."""
    if v["data_stale"]:
        return (f"Index data is stale. Policy tier {v['policy_tier']} retained, new "
                f"purchases blocked, and no forced regime sales generated.")
    if not v["breadth_usable"]:
        return ("Breadth is unusable, so it can neither trigger forced selling nor permit "
                "Risk-On. The tier is decided by structure and the momentum sentinel.")
    if not v["forced_actions_permitted"]:
        return ("No prior policy state exists, so forced selling is suppressed. Proposed "
                "actions are advisory until a tier is committed.")
    return None


def portfolio_block(conn) -> dict:
    """The ACTUAL book, from the most recent stored EOD snapshot.

    Real holdings, real prices, real cost basis. Untouchable instruments are listed but
    flagged, because they are part of the account yet deliberately outside the strategy.
    """
    rows = DB.snapshot_series(conn)
    if not rows:
        return {"available": False}
    latest = DB.get_snapshot(conn, rows[-1]["date"])
    blob = json.loads(latest["holdings_json"] or "{}")
    positions = blob.get("positions", [])
    nav = float(latest["nav"] or 0.0)

    out = []
    for p in positions:
        value = float(p.get("value") or 0.0)
        avg = float(p.get("average_price") or 0.0)
        px = float(p.get("price") or 0.0)
        pnl_pct = ((px / avg - 1.0) * 100.0) if avg > 0 else None
        out.append({
            "symbol": p.get("symbol"), "quantity": p.get("quantity"),
            "average_price": avg, "price": px, "value": value,
            "pledged_qty": p.get("pledged_qty", 0),
            "excluded": bool(p.get("excluded")),
            "weight_pct": (None if p.get("excluded")
                           else (value / nav * 100.0) if nav else None),
            "pnl_pct": pnl_pct,
            "pnl_value": (px - avg) * float(p.get("quantity") or 0) if avg > 0 else None,
        })
    tradeable = [p for p in out if not p["excluded"]]
    winners = [p for p in tradeable if (p["pnl_pct"] or 0) > 0]
    return {
        "available": True,
        "as_of": latest["date"],
        "nav": nav, "invested": float(latest["invested"] or 0.0),
        "cash": float(latest["cash"] or 0.0),
        "cash_pct": (float(latest["cash"] or 0.0) / nav * 100.0) if nav else None,
        "count": len(tradeable), "excluded_count": len(out) - len(tradeable),
        "excluded_value": sum(p["value"] for p in out if p["excluded"]),
        "positions": sorted(out, key=lambda p: -p["value"]),
        "tradeable": sorted(tradeable, key=lambda p: -p["value"]),
        "max_weight": max((p["weight_pct"] or 0) for p in tradeable) if tradeable else 0,
        "max_abs_pnl": max((abs(p["pnl_pct"] or 0) for p in tradeable), default=0),
        "winners": len(winners), "losers": len(tradeable) - len(winners),
        "total_pnl": sum(p["pnl_value"] or 0 for p in tradeable),
        "pledged_count": len([p for p in tradeable if p["pledged_qty"]]),
    }


def transition_history(conn, cfg, limit: int = 12) -> list[dict]:
    """One row per WEEK, not per evaluation.

    An evaluation is re-previewed every time the page or the daily job runs, so the raw
    table held five rows for a single week and the timeline read as five weeks of
    history. A committed decision wins its week; otherwise the newest preview does.
    """
    rows = conn.execute(
        "SELECT e.*, x.actual_equity_pct FROM regime_evaluations e "
        "LEFT JOIN (SELECT evaluation_id, actual_equity_pct, MAX(observed_at) mo "
        "           FROM regime_exposure GROUP BY evaluation_id) x "
        "  ON x.evaluation_id = e.evaluation_id "
        "ORDER BY e.scheduled_week_end DESC, (e.run_id = ?) DESC, e.id DESC",
        (RS.CANONICAL,)).fetchall()
    seen: set = set()
    deduped = []
    for r in rows:
        if r["scheduled_week_end"] in seen:
            continue
        seen.add(r["scheduled_week_end"])
        deduped.append(r)
        if len(deduped) >= limit:
            break
    rows = deduped

    out = []
    for r in rows:
        codes = json.loads(r["reason_codes_json"] or "[]")
        trigger = next((c for c in codes if c in AFFIRMATIVE_RISK_OFF), None) \
            or next((c for c in codes if c.startswith("R1_") or c == "R2_DEFAULT"), "—")
        out.append({"week": r["scheduled_week_end"], "raw": r["raw_candidate_tier"],
                    "tier": r["policy_tier"],
                    "cap": cfg.cap_for(RegimeTier(r["policy_tier"])),
                    "actual": r["actual_equity_pct"], "trigger": trigger,
                    "committed": r["run_id"] == RS.CANONICAL,
                    "breadth": r["breadth_pct"],
                    "transition_limited": bool(r["transition_limited"])})
    out.reverse()          # oldest first, so a timeline reads left to right
    return out


# =====================================================================================
# build
# =====================================================================================
TIMELINE_MIN_WEEKS = 3


def timeline_chart(history: list[dict], *, width: int = 700, height: int = 170,
                   pad_l: int = 40, pad_r: int = 46, pad_t: int = 12,
                   pad_b: int = 26) -> dict | None:
    """Cap, actual exposure and breadth on one 0-100% axis, week by week.

    All three are percentages, so they share an axis honestly — no second scale. The cap
    is drawn as a STEP because it changes only at a tier transition and holds between
    evaluations; a sloped line would imply the limit drifted during the week.

    Returns None below TIMELINE_MIN_WEEKS: two points make a line that looks like a trend
    and is not one. The table beneath carries the same numbers meanwhile.
    """
    rows = [h for h in history if h.get("week")]
    if len({h["week"] for h in rows}) < TIMELINE_MIN_WEEKS:
        return None

    n = max(len(rows) - 1, 1)
    pw, ph = width - pad_l - pad_r, height - pad_t - pad_b

    def px(i): return pad_l + i / n * pw
    def py(v): return pad_t + (1 - max(0.0, min(100.0, float(v))) / 100.0) * ph

    def line(key, step=False):
        pts, pen = [], "M"
        for i, h in enumerate(rows):
            v = h.get(key)
            if v is None:
                pen = "M"
                continue
            if step and pts and pen == "L":
                pts.append(f"L{px(i):.1f},{py(rows[i-1][key]):.1f}")
            pts.append(f"{pen}{px(i):.1f},{py(v):.1f}")
            pen = "L"
        return " ".join(pts)

    return {
        "width": width, "height": height, "pad_l": pad_l, "pad_t": pad_t,
        "weeks": len(rows),
        "cap": line("cap", step=True),
        "actual": line("actual"),
        "breadth": line("breadth"),
        "ticks": [{"v": v, "px": round(py(v), 1)} for v in (0, 50, 100)],
        "xticks": [{"px": round(px(i), 1), "label": h["week"][5:]}
                   for i, h in enumerate(rows)
                   if len(rows) <= 8 or i % max(1, len(rows) // 6) == 0],
        "first": rows[0]["week"], "last": rows[-1]["week"],
        # Stated rather than silently omitted: the sentinel's distance from its 50-DMA is
        # not stored per evaluation, so it cannot be drawn here without inventing it.
        "omitted": "Momentum 50 distance is not stored per evaluation",
    }


def _store_status(conn) -> dict:
    """Cache and database health, plus when data was last successfully collected.

    The per-index freshness above says whether the SIGNAL inputs are current. This says
    whether the machine that fetches them is working at all — a page can show perfectly
    fresh indices while the collection job has been failing for a week on an expired
    token, and the two facts have different fixes.
    """
    out: dict = {"schema_version": None, "journal_mode": None, "db_bytes": None,
                 "last_collection": None, "last_collection_outcome": None,
                 "consecutive_failures": 0, "last_index_write": None}
    try:
        out["schema_version"] = conn.execute("PRAGMA user_version").fetchone()[0]
        out["journal_mode"] = conn.execute("PRAGMA journal_mode").fetchone()[0]
        page = conn.execute("PRAGMA page_size").fetchone()[0]
        count = conn.execute("PRAGMA page_count").fetchone()[0]
        out["db_bytes"] = int(page) * int(count)
        row = conn.execute("SELECT MAX(updated_at) m FROM index_series").fetchone()
        out["last_index_write"] = row["m"] if row else None
    except Exception:
        pass
    try:
        from . import daily_runs as DR
        st = DR.status(conn)
        out["last_collection"] = st.get("ran_at")
        out["last_collection_outcome"] = st.get("outcome")
        out["consecutive_failures"] = st.get("consecutive_failures", 0)
        out["collection_healthy"] = st.get("healthy", False)
    except Exception:
        out["collection_healthy"] = None
    return out


def _plan_link(conn, evaluation_id: str | None) -> dict | None:
    """The stored rebalance plan for this decision, with its reconciliation.

    Falls back to the most recent plan when none carries this evaluation id, labelled so
    the page cannot imply a plan was built under a decision it predates.
    """
    from . import plan_store as PS

    try:
        row = PS.latest(conn, evaluation_id=evaluation_id) if evaluation_id else None
        linked = row is not None
        if row is None:
            row = PS.latest(conn)
        if row is None:
            return None
        recon = PS.reconciliation(conn, row["version_id"]) or {}
        return {**recon, "plan_id": row["version_id"], "linked": linked,
                "created_at": row["created_ts"], "note": row["note"],
                "evaluation_id": row["evaluation_id"]}
    except Exception:
        return None


def build(conn) -> dict:
    cfg = C.regime_config()
    ev, committed = latest_evaluation(conn)
    if not ev:
        return dict(EMPTY, dry_run=C.DRY_RUN, mode=C.REGIME_MODE,
                    regime_enabled=C.REGIME_ENABLED,
                    how_to="python -m app.analytics.snapshot   then run an evaluation")

    snapshot = json.loads(ev.get("input_snapshot_json") or "{}")
    diagnostics = snapshot.get("index_diagnostics", {})
    health = snapshot.get("health", {})
    codes = json.loads(ev.get("reason_codes_json") or "[]")
    reasons = json.loads(ev.get("reasons_json") or "[]")
    weights = json.loads(ev.get("book_weights_json") or "{}")
    plan = snapshot.get("plan", {})
    proposed = snapshot.get("proposed_orders", [])
    manual_items = snapshot.get("manual_review_items", [])
    no_plan_reason = snapshot.get("no_plan_reason")

    as_of = dt.date.fromisoformat(ev["data_as_of"]) if ev.get("data_as_of") else None
    exposure = _row_to_dict(RS.latest_exposure(conn, ev["evaluation_id"]))
    breadth_row = B.latest_reading(conn)
    names = list(cfg.structural_indices.values()) + [cfg.momentum_sentinel]
    status_rows = index_status(conn, names, as_of)

    # The cap belongs to the TIER. A reconciliation row can outlive the decision it was
    # written for, and a stale cap beside a current tier misstates the gap.
    tier = ev.get("policy_tier")
    tier_cap = cfg.cap_for(RegimeTier(tier)) if tier else None
    stored_cap = exposure.get("target_equity_cap_pct")
    cap_mismatch = (stored_cap is not None and tier_cap is not None
                    and abs(stored_cap - tier_cap) > 1e-6)
    actual = exposure.get("actual_equity_pct")
    gap = round(actual - tier_cap, 4) if (actual is not None and tier_cap is not None) else None
    reduction = max(gap, 0.0) if gap is not None else None

    breadth_pct = ev.get("breadth_pct")
    breadth_cov = ev.get("breadth_coverage_pct")
    breadth_usable = bool(breadth_pct is not None and breadth_cov is not None
                          and breadth_cov >= cfg.min_breadth_coverage_pct)
    band = next((b for b in BREADTH_BANDS
                 if breadth_pct is not None and b[0] <= breadth_pct < b[1]), None)

    order = list(cfg.structural_indices.values()) + [cfg.momentum_sentinel]
    cards = _index_cards(diagnostics, cfg, status_rows,
                         _spark_series(conn, cfg, order))
    sentinel_card = next((c for c in cards if c["is_sentinel"]), None)
    mas = (sentinel_card or {}).get("mas", [])
    s50 = next((m for m in mas if m["length"] == 50), {})
    s200 = next((m for m in mas if m["length"] == 200), {})
    veto_active = s50.get("state") == "below"

    any_stale = bool(ev.get("data_stale")) or any(r["stale"] for r in status_rows)
    tax_incomplete = any("TAX_DATA_UNKNOWN" in (m.get("codes") or []) for m in manual_items)
    human, key, meaning = TIER_META.get(tier, (tier or "—", "caution", ""))

    v: dict[str, Any] = {
        "available": True, "committed": committed, "run_id": ev.get("run_id"),
        "dry_run": bool(ev.get("dry_run", C.DRY_RUN)),
        "mode": ev.get("mode", C.REGIME_MODE), "regime_enabled": C.REGIME_ENABLED,

        # 1 · status bar
        "signal_session_date": ev.get("signal_session_date"),
        "scheduled_week_end": ev.get("scheduled_week_end"),
        "data_as_of": ev.get("data_as_of"),
        "next_evaluation_date": ev.get("next_evaluation_date"),
        "last_transition_date": ev.get("last_transition_date"),
        "algorithm_version": ev.get("algorithm_version"),
        "config_hash": ev.get("config_hash"),

        # 2 · regime
        "policy_tier": tier, "tier_name": human, "tier_key": key, "tier_meaning": meaning,
        "raw_candidate_tier": ev.get("raw_candidate_tier"),
        "raw_candidate_name": TIER_META.get(ev.get("raw_candidate_tier"), ("—",))[0],
        "previous_policy_tier": ev.get("previous_policy_tier"),
        "transition_limited": bool(ev.get("transition_limited")),
        "new_buys": ev.get("new_buys"),
        "new_buys_label": NEW_BUY_LABEL.get(ev.get("new_buys"), "—"),
        "forced_action": ev.get("forced_action"),
        "forced_action_label": FORCED_LABEL.get(ev.get("forced_action"), "—"),
        "override_active": bool(ev.get("override_active")),
        "sentinel_veto": veto_active,
        "manual_action_required": bool(ev.get("manual_action_required")),

        # 3 · exposure
        "target_equity_cap_pct": tier_cap, "actual_equity_pct": actual,
        "exposure_gap_pct": gap, "required_reduction_pct": reduction,
        "current_cash_pct": round(100.0 - actual, 4) if actual is not None else None,
        "pending_buy_pct": exposure.get("pending_buy_pct"),
        "pending_sell_pct": exposure.get("pending_sell_pct"),
        "execution_status": exposure.get("execution_status", "not_planned"),
        "exposure_observed_at": exposure.get("observed_at"),
        "exposure_cap_mismatch": cap_mismatch,
        "bar": {"within_cap": min(actual, tier_cap)
                if (actual is not None and tier_cap is not None) else None,
                "excess": reduction,
                "cash": round(100.0 - actual, 4) if actual is not None else None,
                "cap_marker": tier_cap},
        "holdings_now": len([o for o in proposed if o.get("qty_now", 0) > 0]) or None,
        "positions_target": plan.get("positions"),
        "half_sized": plan.get("half_sized", []),
        "target_position_range": list(C.TARGET_POSITIONS),
        "cluster_weights": plan.get("cluster_weights", {}),
        "cluster_cap": C.CLUSTER_CAP,
        "largest_position": plan.get("largest_position"),
        "allocation_feasible": (plan.get("infeasible_reason") is None) if plan else None,
        "allocation_notes": plan.get("allocation_notes", []),
        "plan_summary": plan,

        # 4 · why
        "gates": _gates(health, breadth_pct, breadth_usable, sentinel_card,
                        snapshot.get("bearish_stack_weight"), cfg, codes),
        "narrative": _narrative(cards, health, breadth_pct, breadth_usable,
                                ev.get("new_buys"), cfg, codes),
        "reason_pairs": [{"code": c, "text": DISPLAY_REASONS.get(c, t),
                          "risk_off": c in AFFIRMATIVE_RISK_OFF}
                         for c, t in zip(codes, reasons or codes)],
        "reason_codes": codes,
        "reasons": reasons or [DISPLAY_REASONS.get(c, c) for c in codes],

        # 5 · index cards
        "index_cards": cards,
        "structural_cards": [c for c in cards if not c["is_sentinel"]],
        "sentinel_card": sentinel_card,

        # 6 · structural health
        "health": {k: health.get(k) for k in ("20", "50", "200")},
        "bearish_stack_weight": snapshot.get("bearish_stack_weight"),
        "book_weights": weights, "book_weight_source": ev.get("book_weight_source"),
        "book_weights_are_defaults": ev.get("book_weight_source") == "config_default",

        # 7 · breadth
        "breadth_pct": breadth_pct, "breadth_coverage_pct": breadth_cov,
        "breadth_usable": breadth_usable,
        "breadth_band": band[2] if band else None,
        "breadth_band_key": band[3] if band else None,
        "breadth_bands": [{"lo": b[0], "hi": b[1], "label": b[2], "key": b[3]}
                          for b in BREADTH_BANDS],
        "breadth_observed": breadth_row.observed_count if breadth_row else None,
        "breadth_eligible": breadth_row.eligible_count if breadth_row else None,
        "breadth_as_of": breadth_row.as_of_date.isoformat() if breadth_row else None,
        "breadth_universe": breadth_row.universe_id if breadth_row else None,
        "breadth_universe_hash": breadth_row.universe_hash if breadth_row else None,
        "breadth_version": breadth_row.calculation_version if breadth_row else None,
        "min_breadth_coverage_pct": cfg.min_breadth_coverage_pct,

        # 8 · sentinel veto
        "veto": {"active": veto_active,
                 "dist_50": s50.get("distance_pct"), "state_50": s50.get("state"),
                 "conf_50": s50.get("confirmation"),
                 "dist_200": s200.get("distance_pct"), "state_200": s200.get("state"),
                 "conf_200": s200.get("confirmation"),
                 "below_200": s200.get("state") == "below",
                 "effect": ("New purchases blocked; candidate tier floored at R2"
                            if veto_active else "No restriction from the sentinel"),
                 "release": (f"{cfg.confirm_days} consecutive closes above the 50-DMA "
                             f"plus the {cfg.buffer_bps / 100:.2f}% buffer")},

        # 9 · proposed actions
        "proposed_orders": proposed,
        "proposed_sells": [o for o in proposed if o.get("delta", 0) < 0],
        "proposed_buys": [o for o in proposed if o.get("delta", 0) > 0],
        "guard_orders": [o for o in proposed if o.get("note") == "guard exit"],
        "no_plan_reason": no_plan_reason,

        # 10 · tax
        "manual_review_items": manual_items,
        "manual_review_count": len(manual_items),
        "tax_data_incomplete": tax_incomplete,

        # 11 · execution
        "evaluation_id": ev.get("evaluation_id"),
        "planned_sell_value": sum(abs(o["delta"]) * o["ref_price"]
                                  for o in proposed if o.get("delta", 0) < 0) or None,
        "planned_buy_value": sum(o["delta"] * o["ref_price"]
                                 for o in proposed if o.get("delta", 0) > 0) or None,

        # 11 · the plan that actually implemented this decision, and what it filled.
        # A tier on screen is a policy; only these say whether the book moved.
        "rebalance_plan": _plan_link(conn, ev.get("evaluation_id")),

        # 13 · data quality
        "index_status": status_rows, "data_stale": any_stale,
        "store_status": _store_status(conn),
        "forced_actions_permitted": (not any_stale
                                     and "BOOTSTRAP_OBSERVE_ONLY" not in codes),

        # 14 · configuration
        "config": {
            "ma_lengths": list(cfg.ma_lengths), "buffer_bps": cfg.buffer_bps,
            "confirm_days": cfg.confirm_days,
            "breadth_thresholds": {"R4 crash": f"< {cfg.r4_breadth_max:.0f}%",
                                   "R3 risk-off": f"< {cfg.r3_breadth_max:.0f}%",
                                   "R1 risk-on": f"≥ {cfg.r1_breadth_min:.0f}%",
                                   "recovery override": f"≥ {cfg.recovery_breadth_min:.0f}%"},
            "health_thresholds": {"R3 H200": f"≤ {cfg.r3_h200_max:.2f}",
                                  "R1 H50": f"≥ {cfg.r1_h50_min:.2f}",
                                  "R1 H200": f"≥ {cfg.r1_h200_min:.2f}",
                                  "R4 H200": f"< {cfg.r4_h200_max:.2f}",
                                  "R4 stack": f"≥ {cfg.r4_bearish_stack_min:.2f}"},
            "exposure_map": dict(cfg.tier_exposure_pct),
            "default_weights": dict(cfg.default_book_weights),
            "ltcg_review_days": C.REGIME_LTCG_REVIEW_DAYS,
            "weekly_evaluation_weekday": cfg.weekly_evaluation_weekday,
            "r4_residual": f"max {C.REGIME_R4_MAX_RESIDUAL_NAMES} names, ordered by "
                           f"{', '.join(C.REGIME_R4_RESIDUAL_ORDERING)}",
            "breadth_required": cfg.breadth_required,
            "min_breadth_coverage_pct": cfg.min_breadth_coverage_pct,
            "index_stale_days": cfg.index_stale_days,
            "whipsaw": "≥20pt reduction, ≥80% restored within 4 evaluations, "
                       "benchmark drawdown < 5%",
            "algorithm_version": cfg.algorithm_version,
            "config_hash": cfg.config_hash(),
        },
    }

    v["portfolio"] = portfolio_block(conn)
    v["tier_ladder"] = [dict(t, current=(t["tier"] == tier),
                             cap=cfg.cap_for(RegimeTier(t["tier"])))
                        for t in TIER_LADDER]
    v["mode_explainer"] = _mode_explainer(C.REGIME_ENABLED,
                                          ev.get("mode", C.REGIME_MODE),
                                          bool(ev.get("dry_run", C.DRY_RUN)))
    v["data_health"] = ("Stale" if any_stale
                        else "Incomplete" if not breadth_usable else "Current")
    v["history"] = transition_history(conn, cfg)
    v["timeline"] = timeline_chart(v["history"])
    v["timeline_min_weeks"] = TIMELINE_MIN_WEEKS
    v["banners"] = _banners(ev, v)
    v["safe_state"] = _safe_state(v)
    return v
