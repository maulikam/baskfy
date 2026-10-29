//! Ports the parts of `baskfy_core.tradebook` the overview calls — the FIFO replay that says
//! whether an instrument's trades add up (`history_for`), and the trade-based XIRR — plus
//! `baskfy_api.broker_trades.fills_by_instrument`.

use std::collections::{BTreeMap, HashMap, HashSet};

use chrono::{DateTime, NaiveDate, Utc};

use super::cash::{CashFlow, xirr};
use super::db;
use super::ledger::DomainResult;
use super::pydec::Dec;
use crate::problems::Problem;
use crate::state::AppState;

/// `TradeFill` — the fields the overview reads.
#[derive(Debug, Clone)]
pub(crate) struct TradeFill {
    pub(crate) is_buy: bool,
    pub(crate) quantity: Dec,
    pub(crate) price: Dec,
    pub(crate) trade_date: NaiveDate,
    pub(crate) trade_id: String,
    pub(crate) executed_at: Option<DateTime<Utc>>,
}

/// `TradeHistory` — the fields the overview reads.
pub(crate) struct TradeHistory {
    pub(crate) reconciles: bool,
}

/// `_ordered`: by date, execution time, buys before sells, trade id. Python cannot compare the
/// naive midnight it substitutes for a missing time with an aware one, so a day that mixes the
/// two is the `TypeError` Python raises.
fn ordered(fills: &[TradeFill]) -> DomainResult<Vec<TradeFill>> {
    let mut by_day: HashMap<NaiveDate, (bool, bool)> = HashMap::new();
    for f in fills {
        let e = by_day.entry(f.trade_date).or_insert((false, false));
        if f.executed_at.is_some() {
            e.0 = true;
        } else {
            e.1 = true;
        }
    }
    if by_day.values().any(|(aware, naive)| *aware && *naive) {
        return Err("can't compare offset-naive and offset-aware datetimes".to_owned());
    }
    let mut out = fills.to_vec();
    out.sort_by(|a, b| {
        a.trade_date
            .cmp(&b.trade_date)
            .then_with(|| a.executed_at.cmp(&b.executed_at))
            .then_with(|| b.is_buy.cmp(&a.is_buy))
            .then_with(|| a.trade_id.cmp(&b.trade_id))
    });
    Ok(out)
}

/// `history_for(fills, held_quantity)` — FIFO lots; reconciles when every sell matched a buy and
/// the net is what is held.
pub(crate) fn history_for(fills: &[TradeFill], held_quantity: &Dec) -> DomainResult<TradeHistory> {
    let mut lots: Vec<Dec> = Vec::new();
    let mut unmatched = Dec::lit("0");
    let mut net = Dec::lit("0");
    for fill in ordered(fills)? {
        if fill.is_buy {
            lots.push(fill.quantity.clone());
            net = net.add(&fill.quantity);
            continue;
        }
        net = net.sub(&fill.quantity);
        let mut remaining = fill.quantity.clone();
        while remaining > Dec::zero() && !lots.is_empty() {
            let take = if lots[0] < remaining {
                lots[0].clone()
            } else {
                remaining.clone()
            };
            lots[0] = lots[0].sub(&take);
            remaining = remaining.sub(&take);
            if lots[0].is_zero() {
                lots.remove(0);
            }
        }
        unmatched = unmatched.add(&remaining);
    }
    Ok(TradeHistory {
        reconciles: unmatched.is_zero() && net == *held_quantity,
    })
}

/// `trade_flows`: one signed flow per trade date (buys negative), in date order.
fn trade_flows(fills: &[TradeFill]) -> Vec<CashFlow> {
    let mut by_date: BTreeMap<NaiveDate, Dec> = BTreeMap::new();
    for fill in fills {
        let value = fill.quantity.mul(&fill.price);
        let signed = if fill.is_buy { value.neg() } else { value };
        let next = by_date
            .get(&fill.trade_date)
            .cloned()
            .unwrap_or_else(|| Dec::lit("0"))
            .add(&signed);
        by_date.insert(fill.trade_date, next);
    }
    by_date
        .into_iter()
        .map(|(on, amount)| CashFlow { on, amount })
        .collect()
}

/// `xirr_since_first_purchase(fills, closing_value=, as_of=)`.
pub(crate) fn xirr_since_first_purchase(
    fills: &[TradeFill],
    closing_value: &Dec,
    as_of: NaiveDate,
) -> DomainResult<Option<Dec>> {
    let mut flows = trade_flows(fills);
    if *closing_value > Dec::zero() {
        flows.push(CashFlow {
            on: as_of,
            amount: closing_value.clone(),
        });
    }
    xirr(&flows)
}

/// `fills_by_instrument(session, user_id=...)`: every resolved trade, grouped by instrument, in
/// the order the database returns them (Python's query has no `ORDER BY` either; the replay sorts).
pub(crate) async fn fills_by_instrument(
    state: &AppState,
    user_id: i64,
) -> Result<Vec<(i64, Vec<TradeFill>)>, Problem> {
    let rows = sqlx::query(
        "SELECT instrument_id, side, quantity, price, trade_date, trade_id, executed_at \
         FROM broker_trade WHERE user_id = $1 AND instrument_id IS NOT NULL",
    )
    .bind(user_id)
    .fetch_all(state.db.pool())
    .await
    .map_err(db::fail)?;
    let mut order: Vec<i64> = Vec::new();
    let mut seen = HashSet::new();
    let mut grouped: HashMap<i64, Vec<TradeFill>> = HashMap::new();
    for row in &rows {
        let instrument_id: i64 = db::get(row, "instrument_id")?;
        let side: String = db::get(row, "side")?;
        let fill = TradeFill {
            is_buy: match side.as_str() {
                "BUY" => true,
                "SELL" => false,
                other => {
                    return Err(super::fail(&format!("'{other}' is not a valid TradeSide")));
                }
            },
            quantity: db::pdec(row, "quantity")?,
            price: db::pdec(row, "price")?,
            trade_date: db::date(row, "trade_date")?,
            trade_id: db::get(row, "trade_id")?,
            executed_at: db::opt_ts(row, "executed_at")?,
        };
        if seen.insert(instrument_id) {
            order.push(instrument_id);
        }
        grouped.entry(instrument_id).or_default().push(fill);
    }
    Ok(order
        .into_iter()
        .map(|id| (id, grouped.remove(&id).unwrap_or_default()))
        .collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fill(buy: bool, q: &str, p: &str, d: u32, id: &str) -> TradeFill {
        TradeFill {
            is_buy: buy,
            quantity: Dec::lit(q),
            price: Dec::lit(p),
            trade_date: NaiveDate::from_ymd_opt(2026, 9, d).unwrap_or_default(),
            trade_id: id.to_owned(),
            executed_at: None,
        }
    }

    #[test]
    fn fifo_replay_reconciles_or_says_why_not() {
        // Ports test_tradebook.py's history_for assertions.
        let fills = [
            fill(true, "10", "100", 1, "a"),
            fill(false, "4", "110", 2, "b"),
        ];
        assert!(
            history_for(&fills, &Dec::lit("6"))
                .map(|h| h.reconciles)
                .unwrap_or(false)
        );
        assert!(
            !history_for(&fills, &Dec::lit("10"))
                .map(|h| h.reconciles)
                .unwrap_or(true)
        );
        // A sell before any buy cannot be matched.
        let early = [fill(false, "1", "1", 1, "a"), fill(true, "1", "1", 2, "b")];
        assert!(
            !history_for(&early, &Dec::lit("0"))
                .map(|h| h.reconciles)
                .unwrap_or(true)
        );
        let flows = trade_flows(&fills);
        assert_eq!(flows.len(), 2);
        assert_eq!(flows[0].amount.to_sci_string(), "-1000");
    }
}
