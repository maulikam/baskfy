//! The read half of `baskfy_api.curated_investments` the curated routers share:
//! `load_investment_for_user`, `list_open_pending`, and the pending-action title/body rules.

use baskfy_db::{OrderedJson, row};
use chrono::{DateTime, Utc};
use sqlx::Row;
use sqlx::postgres::PgRow;

use super::{db, dbe, scoped_sole_user_id};
use crate::problems::{Problem, not_found};
use crate::state::AppState;

/// One `cb_investment` row.
#[derive(Debug, Clone)]
pub struct InvestmentRow {
    /// `id`.
    pub id: i64,
    /// `user_id`.
    pub user_id: i64,
    /// `basket_id`.
    pub basket_id: i64,
    /// `status`.
    pub status: String,
    /// `created_at`.
    pub created_at: DateTime<Utc>,
    /// `last_invested_at`.
    pub last_invested_at: Option<DateTime<Utc>>,
    /// `broker_account_id`.
    pub broker_account_id: i64,
    /// `portfolio_id`.
    pub portfolio_id: Option<i64>,
}

/// The columns [`investment_row`] reads.
pub const INVESTMENT_COLUMNS: &str =
    "id, user_id, basket_id, status, created_at, last_invested_at, broker_account_id, portfolio_id";

/// Decodes one `cb_investment` row.
///
/// # Errors
/// 500 for a decode failure.
pub fn investment_row(r: &PgRow) -> Result<InvestmentRow, Problem> {
    Ok(InvestmentRow {
        id: r.try_get("id").map_err(db)?,
        user_id: r.try_get("user_id").map_err(db)?,
        basket_id: r.try_get("basket_id").map_err(db)?,
        status: r.try_get("status").map_err(db)?,
        created_at: row::timestamptz(r, "created_at").map_err(|e| dbe(&e))?,
        last_invested_at: row::opt_timestamptz(r, "last_invested_at").map_err(|e| dbe(&e))?,
        broker_account_id: r.try_get("broker_account_id").map_err(db)?,
        portfolio_id: r.try_get("portfolio_id").map_err(db)?,
    })
}

/// `load_investment_for_user(session, principal_user_id=, investment_id=)`: the sole-tenant gate,
/// then the investment of that tenant or `not_found("investment", str(id))`.
///
/// # Errors
/// The sole-tenant refusals; 404; 500.
pub async fn load_investment_for_user(
    state: &AppState,
    principal_user_id: Option<i64>,
    investment_id: Option<i64>,
) -> Result<InvestmentRow, Problem> {
    let user_id = scoped_sole_user_id(state, principal_user_id, None).await?;
    // `None`: an id beyond bigint, which asyncpg cannot bind — the catch-all 500, after the gate.
    let investment_id = investment_id.ok_or_else(crate::problems::internal_error)?;
    let found = sqlx::query(&format!(
        "SELECT {INVESTMENT_COLUMNS} FROM cb_investment WHERE id = $1 AND user_id = $2"
    ))
    .bind(investment_id)
    .bind(user_id)
    .fetch_optional(state.db.pool())
    .await
    .map_err(db)?;
    match found {
        Some(r) => investment_row(&r),
        None => Err(not_found("investment", &investment_id.to_string())),
    }
}

/// One open `cb_pending_action`.
#[derive(Debug, Clone)]
pub struct PendingRow {
    /// `id`.
    pub id: i64,
    /// `type`.
    pub kind: String,
    /// `payload` (jsonb).
    pub payload: OrderedJson,
    /// `created_at`.
    pub created_at: DateTime<Utc>,
}

/// `list_open_pending(session, user_id)`: neither dismissed nor resolved, newest first.
///
/// # Errors
/// 500 for a database failure.
pub async fn list_open_pending(state: &AppState, user_id: i64) -> Result<Vec<PendingRow>, Problem> {
    let rows = sqlx::query(
        "SELECT id, type, payload, created_at FROM cb_pending_action WHERE user_id = $1 \
         AND dismissed_at IS NULL AND resolved_at IS NULL ORDER BY created_at DESC",
    )
    .bind(user_id)
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    rows.iter()
        .map(|r| {
            Ok(PendingRow {
                id: r.try_get("id").map_err(db)?,
                kind: r.try_get("type").map_err(db)?,
                payload: row::jsonb(r, "payload").map_err(|e| dbe(&e))?,
                created_at: row::timestamptz(r, "created_at").map_err(|e| dbe(&e))?,
            })
        })
        .collect()
}

/// `str.strip()` — Python's whitespace (`str.isspace`), which also counts `\x1c`–`\x1f`.
#[must_use]
pub fn py_strip(s: &str) -> &str {
    s.trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
}

/// `_TITLE_FOR_TYPE.get(type, "Action needed")`.
#[must_use]
pub fn title_for_type(kind: &str) -> &'static str {
    match kind {
        "DRIFT" => "Incorrect holdings — Fix now",
        "REBALANCE_AVAILABLE" => "Rebalance update available",
        "SIP_DUE" => "SIP due",
        _ => "Action needed",
    }
}

fn payload_str<'a>(payload: &'a OrderedJson, key: &str) -> Option<&'a str> {
    match payload {
        OrderedJson::Object(_) => match payload.get(key) {
            Some(OrderedJson::Str(s)) if !py_strip(s).is_empty() => Some(py_strip(s)),
            _ => None,
        },
        _ => None,
    }
}

/// `_title_for` / the title half of `_pending_brief`.
#[must_use]
pub fn pending_title(p: &PendingRow) -> String {
    payload_str(&p.payload, "title")
        .map_or_else(|| title_for_type(&p.kind).to_owned(), str::to_owned)
}

/// `_body_for` / the body half of `_pending_brief`.
#[must_use]
pub fn pending_body(p: &PendingRow) -> Option<String> {
    ["body", "body_md", "message", "summary"]
        .iter()
        .find_map(|k| payload_str(&p.payload, k))
        .map(str::to_owned)
}

// ---------------------------------------------------------------------------------------------
// Investor math: `curated_investments.snapshot_dict` over `baskfy_core.curated_accounting`
// ---------------------------------------------------------------------------------------------

use baskfy_core::decimal::{Rounding, WideDecimal};
use rust_decimal::Decimal;

/// `money(value)`: quantise to paise, half-up (`baskfy_core.gst.money`).
///
/// # Errors
/// 500 when the value cannot be quantised (more than 28 digits, Python's `InvalidOperation`).
pub fn money(value: &WideDecimal) -> Result<Decimal, Problem> {
    value
        .quantize(-2, Rounding::HalfUp)
        .and_then(|q| q.try_to_decimal())
        .map_err(|e| {
            crate::logging::error(
                "baskfy_api.app",
                "unhandled error",
                &[("error", &e.to_string())],
            );
            crate::problems::internal_error()
        })
}

/// A `ValueError` raised inside the investor math: an unhandled exception, the 500.
fn value_error(message: &str) -> Problem {
    crate::logging::error("baskfy_api.app", "unhandled error", &[("error", message)]);
    crate::problems::internal_error()
}

/// One `cb_investment_holding`.
#[derive(Debug, Clone, Copy)]
pub struct Holding {
    /// `instrument_id`.
    pub instrument_id: i64,
    /// `qty`.
    pub qty: Decimal,
    /// `avg_price`.
    pub avg_price: Decimal,
}

/// The investor snapshot (`SnapshotOut` / `CostsSnapshotOut` are the same fields).
#[derive(Debug, Clone, serde::Serialize)]
pub struct SnapshotOut {
    /// `money_put_in`.
    #[serde(serialize_with = "crate::wire::ser::decimal")]
    pub money_put_in: Decimal,
    /// `current_investment`.
    #[serde(serialize_with = "crate::wire::ser::decimal")]
    pub current_investment: Decimal,
    /// `current_value`.
    #[serde(serialize_with = "crate::wire::ser::decimal")]
    pub current_value: Decimal,
    /// `current_returns`.
    #[serde(serialize_with = "crate::wire::ser::decimal")]
    pub current_returns: Decimal,
    /// `current_returns_pct`.
    #[serde(serialize_with = "crate::wire::ser::decimal")]
    pub current_returns_pct: Decimal,
    /// `realized_pnl`.
    #[serde(serialize_with = "crate::wire::ser::decimal")]
    pub realized_pnl: Decimal,
    /// `dividends`.
    #[serde(serialize_with = "crate::wire::ser::decimal")]
    pub dividends: Decimal,
    /// `xirr` — always `None` from `snapshot_dict`.
    #[serde(serialize_with = "crate::wire::ser::opt_decimal")]
    pub xirr: Option<Decimal>,
    /// `xirr_displayable`.
    pub xirr_displayable: bool,
    /// `marked_at_cost`.
    pub marked_at_cost: bool,
}

/// `snapshot_dict(buy_amounts=, holdings=, prices=, first_invested=, as_of=)` with `marked_at_cost`
/// set as `_snapshot_for` / `get_investment_costs` set it.
///
/// # Errors
/// 500 for a negative amount, quantity or price (`ValueError` in Python), or a missing price.
pub fn snapshot(
    buy_amounts: &[Decimal],
    holdings: &[Holding],
    prices: &std::collections::HashMap<i64, Decimal>,
    first_invested: chrono::NaiveDate,
    as_of: chrono::NaiveDate,
    marked_at_cost: bool,
) -> Result<SnapshotOut, Problem> {
    // money_put_in
    let mut total = WideDecimal::zero();
    for amount in buy_amounts {
        if amount.is_sign_negative() && !amount.is_zero() {
            return Err(value_error("buy-side amount cannot be negative"));
        }
        total = total.add(&WideDecimal::from(*amount));
    }
    let put_in = money(&total)?;
    // current_investment(put_in, 0)
    let invested = money(&WideDecimal::from(put_in).sub(&WideDecimal::zero()))?;
    // current_value(holdings, prices)
    let mut value = WideDecimal::zero();
    for h in holdings {
        if h.qty.is_sign_negative() && !h.qty.is_zero() {
            return Err(value_error("holding qty cannot be negative"));
        }
        let price = prices
            .get(&h.instrument_id)
            .copied()
            .ok_or_else(|| value_error("KeyError: price"))?;
        if price.is_sign_negative() && !price.is_zero() {
            return Err(value_error("price cannot be negative"));
        }
        value = value.add(&WideDecimal::from(h.qty).mul(&WideDecimal::from(price)));
    }
    let current_value = money(&value)?;
    let diff = WideDecimal::from(current_value).sub(&WideDecimal::from(invested));
    let returns_abs = money(&diff)?;
    let returns_pct = if invested.is_zero() {
        money(&WideDecimal::zero())?
    } else {
        let ratio = diff
            .div(&WideDecimal::from(invested))
            .map_err(|e| value_error(&e.to_string()))?;
        money(&ratio)?
    };
    let displayable = as_of >= first_invested && (as_of - first_invested).num_days() > 365;
    Ok(SnapshotOut {
        money_put_in: put_in,
        current_investment: invested,
        current_value,
        current_returns: returns_abs,
        current_returns_pct: returns_pct,
        realized_pnl: money(&WideDecimal::zero())?,
        dividends: money(&WideDecimal::zero())?,
        xirr: None,
        xirr_displayable: displayable,
        marked_at_cost,
    })
}

/// `_snapshot_for` / the costs route's marking: holdings at a live quote when one is positive,
/// else at average cost (`marked_at_cost`); no holdings at all is not "marked at cost".
#[must_use]
pub fn marked_prices(
    holdings: &[Holding],
    live: &std::collections::HashMap<i64, Decimal>,
) -> (std::collections::HashMap<i64, Decimal>, bool) {
    // `{instrument_id: avg_price}`: a repeated instrument keeps its last average price, and its
    // first position in the dict (Python's dict comprehension).
    let mut order: Vec<i64> = Vec::new();
    let mut prices = std::collections::HashMap::new();
    for h in holdings {
        if prices.insert(h.instrument_id, h.avg_price).is_none() {
            order.push(h.instrument_id);
        }
    }
    let mut marked_at_cost = false;
    for id in &order {
        match live.get(id) {
            Some(q) if *q > Decimal::ZERO => {
                prices.insert(*id, *q);
            }
            _ => marked_at_cost = true,
        }
    }
    if prices.is_empty() {
        marked_at_cost = false;
    }
    (prices, marked_at_cost)
}

/// `live_prices.live_prices_by_instrument(session, ids)` as far as this wave can answer it:
/// Python overlays the broker book's `last_price` (a live Kite holdings read) and then read-only
/// quotes, both only with a real Kite session. Neither the session reader nor the holdings read
/// is ported, so this is `{}` — exactly Python's answer with no session (the bed, and any host
/// without Kite) and the answer `routes::reference::quotes_permitted` gives with a key
/// configured (DECISIONS-RS R5.11).
#[must_use]
pub fn live_prices_by_instrument(_ids: &[i64]) -> std::collections::HashMap<i64, Decimal> {
    std::collections::HashMap::new()
}

/// Reads the holdings of one investment (no `ORDER BY`, as Python reads them).
///
/// # Errors
/// 500 for a database failure.
pub async fn holdings_of(
    state: &AppState,
    investment_ids: &[i64],
) -> Result<Vec<(i64, Holding)>, Problem> {
    if investment_ids.is_empty() {
        return Ok(Vec::new());
    }
    let rows = sqlx::query(
        "SELECT investment_id, instrument_id, qty, avg_price FROM cb_investment_holding \
         WHERE investment_id = ANY($1)",
    )
    .bind(investment_ids)
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    rows.iter()
        .map(|r| {
            Ok((
                r.try_get::<i64, _>("investment_id").map_err(db)?,
                Holding {
                    instrument_id: r.try_get("instrument_id").map_err(db)?,
                    qty: row::decimal(r, "qty").map_err(|e| dbe(&e))?,
                    avg_price: row::decimal(r, "avg_price").map_err(|e| dbe(&e))?,
                },
            ))
        })
        .collect()
}

/// One `cb_order_batch`.
#[derive(Debug, Clone)]
pub struct Batch {
    /// `id`.
    pub id: i64,
    /// `investment_id`.
    pub investment_id: i64,
    /// `kind`.
    pub kind: String,
    /// `status`.
    pub status: String,
    /// `requested_amount`.
    pub requested_amount: Option<Decimal>,
    /// `desk_plan_id`.
    pub desk_plan_id: Option<String>,
    /// `created_at`.
    pub created_at: DateTime<Utc>,
}

/// Reads the order batches of some investments, in the SQL order given (`""` = Python's
/// unordered read).
///
/// # Errors
/// 500 for a database failure.
pub async fn batches_of(
    state: &AppState,
    investment_ids: &[i64],
    order: &str,
) -> Result<Vec<Batch>, Problem> {
    if investment_ids.is_empty() {
        return Ok(Vec::new());
    }
    let rows = sqlx::query(&format!(
        "SELECT id, investment_id, kind, status, requested_amount, desk_plan_id, created_at \
         FROM cb_order_batch WHERE investment_id = ANY($1) {order}"
    ))
    .bind(investment_ids)
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    rows.iter()
        .map(|r| {
            Ok(Batch {
                id: r.try_get("id").map_err(db)?,
                investment_id: r.try_get("investment_id").map_err(db)?,
                kind: r.try_get("kind").map_err(db)?,
                status: r.try_get("status").map_err(db)?,
                requested_amount: row::opt_decimal(r, "requested_amount").map_err(|e| dbe(&e))?,
                desk_plan_id: r.try_get("desk_plan_id").map_err(db)?,
                created_at: row::timestamptz(r, "created_at").map_err(|e| dbe(&e))?,
            })
        })
        .collect()
}

/// `[b.requested_amount for b in batches if b.kind in BUY/INVEST_MORE/SIP and amount is not None]
/// or [Decimal("0")]`.
#[must_use]
pub fn buy_amounts(batches: &[&Batch]) -> Vec<Decimal> {
    let amounts: Vec<Decimal> = batches
        .iter()
        .filter(|b| matches!(b.kind.as_str(), "BUY" | "INVEST_MORE" | "SIP"))
        .filter_map(|b| b.requested_amount)
        .collect();
    if amounts.is_empty() {
        vec![Decimal::ZERO]
    } else {
        amounts
    }
}

/// Python's `payload.get("basket_id") == basket_id` for a JSON value (an int, a whole float, or a
/// bool — `True == 1` — compare equal).
#[must_use]
pub fn json_equals_int(value: Option<&OrderedJson>, n: i64) -> bool {
    match value {
        Some(OrderedJson::Int(i)) => *i == n,
        Some(OrderedJson::UInt(u)) => i64::try_from(*u).is_ok_and(|u| u == n),
        #[allow(clippy::cast_precision_loss, clippy::float_cmp)]
        Some(OrderedJson::Float(f)) => *f == n as f64,
        Some(OrderedJson::Bool(b)) => i64::from(*b) == n,
        _ => false,
    }
}

/// Python's `isinstance(raw, int)` (bools included) for a JSON value, as the int.
#[must_use]
pub fn json_int(value: Option<&OrderedJson>) -> Option<i64> {
    match value {
        Some(OrderedJson::Int(i)) => Some(*i),
        Some(OrderedJson::UInt(u)) => i64::try_from(*u).ok(),
        Some(OrderedJson::Bool(b)) => Some(i64::from(*b)),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;
    use std::collections::HashMap;
    use std::str::FromStr;

    fn d(s: &str) -> Decimal {
        Decimal::from_str(s).unwrap()
    }

    fn snap_with(live: &HashMap<i64, Decimal>) -> SnapshotOut {
        let holdings = [Holding {
            instrument_id: 1,
            qty: d("10"),
            avg_price: d("100"),
        }];
        let (prices, marked) = marked_prices(&holdings, live);
        snapshot(
            &[d("1000")],
            &holdings,
            &prices,
            chrono::NaiveDate::from_ymd_opt(2026, 1, 1).unwrap(),
            chrono::NaiveDate::from_ymd_opt(2026, 9, 12).unwrap(),
            marked,
        )
        .unwrap()
    }

    #[test]
    fn a_snapshot_is_marked_at_cost_without_live_quotes() {
        // Ports test_curated_marked_at_cost.py.
        let snap = snap_with(&HashMap::new());
        assert!(snap.marked_at_cost);
        assert_eq!(snap.current_value, d("1000"));
        assert_eq!(snap.current_value.to_string(), "1000.00");
        assert_eq!(snap.realized_pnl.to_string(), "0.00");
        assert!(!snap.xirr_displayable);
        let snap = snap_with(&[(1, d("120"))].into());
        assert!(!snap.marked_at_cost);
        assert_eq!(snap.current_value, d("1200"));
        assert_eq!(snap.current_returns_pct.to_string(), "0.20");
    }

    #[test]
    fn titles_and_bodies_prefer_the_payload_then_the_type() {
        // Ports test_curated_engage.py::test_title_and_body_prefer_payload_then_type.
        let mut p = PendingRow {
            id: 1,
            kind: "REBALANCE_AVAILABLE".to_owned(),
            payload: OrderedJson::parse(r#"{"title": "Apply v3", "body": "Two names changed"}"#)
                .unwrap(),
            created_at: DateTime::<Utc>::default(),
        };
        assert_eq!(pending_title(&p), "Apply v3");
        assert_eq!(pending_body(&p).as_deref(), Some("Two names changed"));
        p.payload = OrderedJson::Object(Vec::new());
        assert!(pending_title(&p).contains("Rebalance"));
        assert_eq!(pending_body(&p), None);
        p.payload = OrderedJson::parse(r#"{"title": "  ", "summary": " s \u001c"}"#).unwrap();
        assert_eq!(pending_title(&p), "Rebalance update available");
        assert_eq!(pending_body(&p).as_deref(), Some("s"));
    }
}
