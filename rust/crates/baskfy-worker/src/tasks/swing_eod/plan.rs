//! The plan's last steps — ports `baskfy_core.swing.plan.exit_lines`, `assemble` and
//! `SwingPlan.plan_hash` (pure; not yet in `baskfy_core::swing::plan`, which ports
//! `build_entries` only — lift them there when the desk wave needs them), and
//! `tasks/swing_eod.store_plan` (the writer).
//!
//! **Law 2.** A stored plan is advice with a 30-minute expiry; nothing here sends it.

use std::fmt::Write as _;

use baskfy_core::swing::market::{ExposureTier, MarketGate};
use baskfy_core::swing::plan::{LineKind, PlanLine, Skipped, to_tick};
use baskfy_core::swing::stops::{Action, ActionKind};
use chrono::{DateTime, Duration, NaiveDate, Utc};
use rust_decimal::Decimal;
use serde::Serialize;
use sha2::{Digest as _, Sha256};
use sqlx::PgConnection;

use crate::error::WorkerError;
use crate::steps::log_warning;

/// `baskfy_core.models.swing.PLAN_TTL_MINUTES`.
pub(super) const PLAN_TTL_MINUTES: i64 = 30;

fn core_err(e: baskfy_core::error::CoreError) -> WorkerError {
    WorkerError::python("ValueError", e.to_string())
}

/// A serde-renamed enum's value (`StrEnum.value`).
pub(super) fn enum_value<T: Serialize>(value: &T) -> String {
    match serde_json::to_value(value) {
        Ok(serde_json::Value::String(text)) => text,
        Ok(other) => other.to_string(),
        Err(e) => e.to_string(),
    }
}

/// `exit_lines(symbol, actions, position_id)`: a sell becomes `SELL_AT_OPEN`, a raise with a new
/// stop `RAISE_GTT_STOP` at the tick; a stop-out and a hold produce no line (the GTT does that
/// work).
pub(super) fn exit_lines(
    symbol: &str,
    actions: &[Action],
    position_id: Option<i64>,
) -> Result<Vec<PlanLine>, WorkerError> {
    let mut lines = Vec::new();
    for action in actions {
        match action.kind {
            ActionKind::SellPartial | ActionKind::SellAll => lines.push(PlanLine {
                kind: LineKind::SellAtOpen,
                symbol: symbol.to_owned(),
                setup: None,
                quantity: action.quantity,
                trigger: None,
                stop: None,
                risk_inr: Decimal::ZERO,
                position_value: Decimal::ZERO,
                trail: None,
                note: enum_value(&action.reason),
                position_id,
            }),
            ActionKind::RaiseStop => {
                if let Some(new_stop) = action.new_stop {
                    lines.push(PlanLine {
                        kind: LineKind::RaiseGttStop,
                        symbol: symbol.to_owned(),
                        setup: None,
                        quantity: 0,
                        trigger: None,
                        stop: Some(to_tick(new_stop).map_err(core_err)?),
                        risk_inr: Decimal::ZERO,
                        position_value: Decimal::ZERO,
                        trail: None,
                        note: enum_value(&action.reason),
                        position_id,
                    });
                }
            }
            ActionKind::StoppedOut | ActionKind::Hold => {}
        }
    }
    Ok(lines)
}

/// `SwingPlan`.
#[derive(Debug, Clone)]
pub(crate) struct SwingPlan {
    pub(super) as_of: NaiveDate,
    pub(super) gate: MarketGate,
    pub(super) tier: ExposureTier,
    pub(super) lines: Vec<PlanLine>,
    pub(super) skipped: Vec<Skipped>,
    pub(super) total_risk_inr: Decimal,
    pub(super) total_new_exposure_inr: Decimal,
}

/// `assemble`: exits first — the money they free is the money the entries spend.
pub(crate) fn assemble(
    as_of: NaiveDate,
    gate: MarketGate,
    tier: ExposureTier,
    entries: Vec<PlanLine>,
    exits: Vec<PlanLine>,
    skipped: Vec<Skipped>,
) -> Result<SwingPlan, WorkerError> {
    let overflow = || WorkerError::python("Overflow", "plan totals overflowed");
    let mut risk = Decimal::ZERO;
    let mut exposure = Decimal::ZERO;
    for line in &entries {
        risk = risk.checked_add(line.risk_inr).ok_or_else(overflow)?;
        exposure = exposure
            .checked_add(line.position_value)
            .ok_or_else(overflow)?;
    }
    let mut lines = exits;
    lines.extend(entries);
    Ok(SwingPlan {
        as_of,
        gate,
        tier,
        lines,
        skipped,
        total_risk_inr: risk,
        total_new_exposure_inr: exposure,
    })
}

/// `json.dumps(text)` with `ensure_ascii=True`.
fn json_str(out: &mut String, text: &str) {
    out.push('"');
    for c in text.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 || (c as u32) > 0x7e => {
                let mut buf = [0_u16; 2];
                for unit in c.encode_utf16(&mut buf) {
                    let _ = write!(out, "\\u{unit:04x}");
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

fn json_opt_dec(out: &mut String, value: Option<Decimal>) {
    match value {
        Some(v) => json_str(out, &v.to_string()),
        None => out.push_str("null"),
    }
}

impl SwingPlan {
    /// `plan_hash()`: sha256 of `json.dumps({as_of, gate, tier, lines}, sort_keys=True,
    /// separators=(",", ":"))`, each line `asdict` with its Decimals as `str`.
    pub(super) fn plan_hash(&self) -> String {
        let mut text = String::new();
        text.push_str("{\"as_of\":");
        json_str(&mut text, &self.as_of.format("%Y-%m-%d").to_string());
        text.push_str(",\"gate\":");
        json_str(&mut text, &enum_value(&self.gate));
        text.push_str(",\"lines\":[");
        for (i, line) in self.lines.iter().enumerate() {
            if i > 0 {
                text.push(',');
            }
            // Keys sorted: kind, note, position_id, position_value, quantity, risk_inr, setup,
            // stop, symbol, trail, trigger.
            text.push_str("{\"kind\":");
            json_str(&mut text, &enum_value(&line.kind));
            text.push_str(",\"note\":");
            json_str(&mut text, &line.note);
            text.push_str(",\"position_id\":");
            match line.position_id {
                Some(id) => {
                    let _ = write!(text, "{id}");
                }
                None => text.push_str("null"),
            }
            text.push_str(",\"position_value\":");
            json_str(&mut text, &line.position_value.to_string());
            let _ = write!(text, ",\"quantity\":{}", line.quantity);
            text.push_str(",\"risk_inr\":");
            json_str(&mut text, &line.risk_inr.to_string());
            text.push_str(",\"setup\":");
            match &line.setup {
                Some(setup) => json_str(&mut text, setup.as_str()),
                None => text.push_str("null"),
            }
            text.push_str(",\"stop\":");
            json_opt_dec(&mut text, line.stop);
            text.push_str(",\"symbol\":");
            json_str(&mut text, &line.symbol);
            text.push_str(",\"trail\":");
            match &line.trail {
                Some(trail) => json_str(&mut text, &enum_value(trail)),
                None => text.push_str("null"),
            }
            text.push_str(",\"trigger\":");
            json_opt_dec(&mut text, line.trigger);
            text.push('}');
        }
        let _ = write!(text, "],\"tier\":{}}}", self.tier.level);
        let digest = Sha256::digest(text.as_bytes());
        let mut hex = String::with_capacity(64);
        for byte in digest {
            let _ = write!(hex, "{byte:02x}");
        }
        hex
    }
}

/// `store_plan`: the plan, its lines (a line whose symbol has no instrument id is skipped with a
/// warning) and its skips. Returns the `plan_id` (a fresh uuid4, as Python's `uuid.uuid4()`).
pub(crate) async fn store_plan(
    conn: &mut PgConnection,
    plan: &SwingPlan,
    user_id: i64,
    source: &str,
    instrument_ids: &[(String, i64)],
    now: DateTime<Utc>,
) -> Result<String, WorkerError> {
    let lookup = |symbol: &str| {
        instrument_ids
            .iter()
            .rev()
            .find(|(s, _)| s == symbol)
            .map(|(_, id)| *id)
    };
    let (plan_uuid,): (String,) = sqlx::query_as("SELECT gen_random_uuid()::text")
        .fetch_one(&mut *conn)
        .await?;
    let (row_id,): (i64,) = sqlx::query_as(
        "INSERT INTO sw_plan (plan_id, user_id, as_of, source, built_at, expires_at, plan_hash, \
         gate, exposure_level, total_risk_inr, total_new_exposure_inr) \
         VALUES (CAST($1 AS UUID), $2, $3, $4, $5, $6, $7, $8, $9, $10, $11) RETURNING id",
    )
    .bind(&plan_uuid)
    .bind(user_id)
    .bind(plan.as_of)
    .bind(source)
    .bind(now)
    .bind(now + Duration::minutes(PLAN_TTL_MINUTES))
    .bind(plan.plan_hash())
    .bind(enum_value(&plan.gate))
    .bind(
        i16::try_from(plan.tier.level)
            .map_err(|e| WorkerError::python("OverflowError", e.to_string()))?,
    )
    .bind(plan.total_risk_inr)
    .bind(plan.total_new_exposure_inr)
    .fetch_one(&mut *conn)
    .await?;
    for line in &plan.lines {
        let Some(instrument_id) = lookup(&line.symbol) else {
            log_warning(&format!(
                "plan line for {} has no instrument id; skipped",
                line.symbol
            ));
            continue;
        };
        let kind = enum_value(&line.kind);
        sqlx::query(
            "INSERT INTO sw_plan_line (plan_id, user_id, kind, instrument_id, setup, quantity, \
             trigger, stop, risk_inr, position_value, trail, note, position_id, state, client_id) \
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, 'PROPOSED', $14)",
        )
        .bind(row_id)
        .bind(user_id)
        .bind(&kind)
        .bind(instrument_id)
        .bind(line.setup.map(|s| s.as_str().to_owned()))
        .bind(
            i32::try_from(line.quantity)
                .map_err(|e| WorkerError::python("OverflowError", e.to_string()))?,
        )
        .bind(line.trigger)
        .bind(line.stop)
        .bind(line.risk_inr)
        .bind(line.position_value)
        .bind(line.trail.as_ref().map(enum_value))
        .bind(&line.note)
        .bind(line.position_id)
        .bind(format!("{plan_uuid}:{}:{kind}", line.symbol))
        .execute(&mut *conn)
        .await?;
    }
    for skip in &plan.skipped {
        sqlx::query(
            "INSERT INTO sw_plan_skip (plan_id, user_id, instrument_id, symbol, reason, detail) \
             VALUES ($1, $2, $3, $4, $5, $6)",
        )
        .bind(row_id)
        .bind(user_id)
        .bind(lookup(&skip.symbol))
        .bind(&skip.symbol)
        .bind(enum_value(&skip.reason))
        .bind(if skip.detail.is_empty() {
            None
        } else {
            Some(skip.detail.clone())
        })
        .execute(&mut *conn)
        .await?;
    }
    Ok(plan_uuid)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;
    use baskfy_core::swing::stops::ActionReason;

    fn tier() -> ExposureTier {
        ExposureTier {
            level: 1,
            max_open_positions: 4,
            max_exposure_pct: 50.0,
            new_entries_allowed: true,
            drawdown_locked: false,
        }
    }

    #[test]
    fn sells_become_sell_at_open_raises_snap_to_the_tick_and_stop_outs_add_nothing() {
        let actions = vec![
            Action {
                kind: ActionKind::SellPartial,
                reason: ActionReason::PartialIntoStrength,
                quantity: 3,
                new_stop: None,
            },
            Action {
                kind: ActionKind::RaiseStop,
                reason: ActionReason::BreakevenAfterPartial,
                quantity: 0,
                new_stop: Some("101.03".parse().unwrap()),
            },
            Action {
                kind: ActionKind::StoppedOut,
                reason: ActionReason::HardStopHit,
                quantity: 10,
                new_stop: None,
            },
            Action {
                kind: ActionKind::RaiseStop,
                reason: ActionReason::BreakevenAtR,
                quantity: 0,
                new_stop: None,
            },
        ];
        let lines = exit_lines("ABC", &actions, Some(7)).unwrap();
        assert_eq!(lines.len(), 2);
        assert_eq!(lines[0].kind, LineKind::SellAtOpen);
        assert_eq!(lines[0].note, "PARTIAL_INTO_STRENGTH");
        assert_eq!(lines[0].quantity, 3);
        assert_eq!(lines[1].kind, LineKind::RaiseGttStop);
        assert_eq!(lines[1].stop, Some("101.05".parse().unwrap()));
        assert_eq!(lines[1].position_id, Some(7));
    }

    #[test]
    fn the_plan_puts_exits_first_and_totals_only_the_entries() {
        let exit = exit_lines(
            "X",
            &[Action {
                kind: ActionKind::SellAll,
                reason: ActionReason::CloseBelowTrailMa,
                quantity: 5,
                new_stop: None,
            }],
            None,
        )
        .unwrap();
        let mut entry = exit[0].clone();
        entry.kind = LineKind::BuyOnTrigger;
        entry.symbol = "Y".to_owned();
        entry.risk_inr = "100.50".parse().unwrap();
        entry.position_value = "2000".parse().unwrap();
        let day = NaiveDate::from_ymd_opt(2026, 10, 29).unwrap();
        let plan = assemble(day, MarketGate::Green, tier(), vec![entry], exit, vec![]).unwrap();
        assert_eq!(plan.lines[0].symbol, "X");
        assert_eq!(plan.total_risk_inr, "100.50".parse::<Decimal>().unwrap());
        assert_eq!(
            plan.total_new_exposure_inr,
            "2000".parse::<Decimal>().unwrap()
        );
    }

    #[test]
    fn the_hash_is_pythons_canonical_json() {
        // hashlib.sha256(json.dumps({"as_of": "2026-10-29", "gate": "RED", "tier": 1,
        //   "lines": []}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        let day = NaiveDate::from_ymd_opt(2026, 10, 29).unwrap();
        let plan = assemble(day, MarketGate::Red, tier(), vec![], vec![], vec![]).unwrap();
        let text = "{\"as_of\":\"2026-10-29\",\"gate\":\"RED\",\"lines\":[],\"tier\":1}";
        let mut hex = String::new();
        for b in Sha256::digest(text.as_bytes()) {
            let _ = write!(hex, "{b:02x}");
        }
        assert_eq!(plan.plan_hash(), hex);
        let mut escaped = String::new();
        json_str(&mut escaped, "a · b — \"c\"");
        assert_eq!(escaped, "\"a \\u00b7 b \\u2014 \\\"c\\\"\"");
    }
}
