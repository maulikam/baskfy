//! `app/analytics/tax_lots.py` — FIFO tax-lot review for regime sells. **Advisory only**, never a
//! risk override: a flag pushes a lot to the back of the sell queue and raises a manual-review
//! item; it never suppresses a guard. Missing data is never "tax-safe": a missing acquisition date
//! and a missing acquisition price both produce `TAX_DATA_UNKNOWN`.

use std::collections::BTreeSet;

use chrono::NaiveDate;

use super::{local_from_ts, pyf, pyi, round_to};
use crate::storage::{Conn, StorageError};
use crate::wire::{PyDict, PyValue, dict};

/// Equity LTCG threshold, days.
pub const LONG_TERM_DAYS: i64 = 365;
/// Days before the threshold inside which a profitable lot is flagged.
pub const DEFAULT_REVIEW_DAYS: i64 = 45;

/// Reason code.
pub const TAX_DATA_UNKNOWN: &str = "TAX_DATA_UNKNOWN";
/// Reason code.
pub const TAX_LOT_NEAR_LTCG: &str = "TAX_LOT_NEAR_LTCG";
/// Reason code.
pub const TAX_LOT_LONG_TERM: &str = "TAX_LOT_LONG_TERM";
/// Reason code.
pub const TAX_LOT_LOSS: &str = "TAX_LOT_LOSS";
/// Reason code.
pub const TAX_MISSING_ACQUISITION_DATE: &str = "TAX_MISSING_ACQUISITION_DATE";
/// Reason code.
pub const TAX_MISSING_ACQUISITION_PRICE: &str = "TAX_MISSING_ACQUISITION_PRICE";
/// Reason code.
pub const TAX_LOTS_INCOMPLETE: &str = "TAX_LOTS_INCOMPLETE";

/// One acquisition lot.
#[derive(Debug, Clone, PartialEq)]
pub struct Lot {
    /// `symbol`.
    pub symbol: String,
    /// `quantity`.
    pub quantity: i64,
    /// `acquired_on`.
    pub acquired_on: Option<NaiveDate>,
    /// `price`.
    pub price: Option<f64>,
    /// `corporate_action_adjusted`.
    pub corporate_action_adjusted: bool,
}

impl Lot {
    /// `complete`: date and price both known.
    #[must_use]
    pub const fn complete(&self) -> bool {
        self.acquired_on.is_some() && self.price.is_some()
    }
}

/// How much of one lot a sale consumes.
#[derive(Debug, Clone, PartialEq)]
pub struct LotConsumption {
    /// The lot.
    pub lot: Lot,
    /// Shares taken from it.
    pub quantity: i64,
    /// `days_held`.
    pub days_held: Option<i64>,
    /// `days_to_ltcg`.
    pub days_to_ltcg: Option<i64>,
    /// `gain`.
    pub gain: Option<f64>,
    /// `long_term`.
    pub long_term: Option<bool>,
    /// `flagged`.
    pub flagged: bool,
    /// Sorted reason codes.
    pub codes: Vec<&'static str>,
}

/// The review of one proposed sale.
#[derive(Debug, Clone, PartialEq)]
pub struct TaxReview {
    /// `symbol`.
    pub symbol: String,
    /// `requested_qty`.
    pub requested_qty: i64,
    /// `covered_qty`.
    pub covered_qty: i64,
    /// The FIFO walk.
    pub consumptions: Vec<LotConsumption>,
    /// `flagged`.
    pub flagged: bool,
    /// `data_unknown`.
    pub data_unknown: bool,
    /// `estimated_gain`.
    pub estimated_gain: Option<f64>,
    /// `flagged_qty`.
    pub flagged_qty: i64,
    /// Sorted reason codes.
    pub codes: Vec<&'static str>,
}

fn codes_py(codes: &[&'static str]) -> PyValue {
    PyValue::List(
        codes
            .iter()
            .map(|c| PyValue::Str((*c).to_owned()))
            .collect(),
    )
}

fn opt<T>(v: Option<T>, f: impl Fn(T) -> PyValue) -> PyValue {
    v.map_or(PyValue::None, f)
}

impl TaxReview {
    /// `manual_review`.
    #[must_use]
    pub const fn manual_review(&self) -> bool {
        self.flagged || self.data_unknown
    }

    /// `as_dict()`.
    #[must_use]
    pub fn as_dict(&self) -> PyDict {
        dict([
            ("symbol", PyValue::Str(self.symbol.clone())),
            ("requested_qty", PyValue::Int(self.requested_qty)),
            ("covered_qty", PyValue::Int(self.covered_qty)),
            ("flagged", PyValue::Bool(self.flagged)),
            ("flagged_qty", PyValue::Int(self.flagged_qty)),
            ("data_unknown", PyValue::Bool(self.data_unknown)),
            ("estimated_gain", opt(self.estimated_gain, PyValue::Float)),
            ("codes", codes_py(&self.codes)),
        ])
    }
}

/// `open_lots(conn, symbol)`: open acquisition lots, oldest first, from `trades`. Incomplete rows
/// are returned (and flagged downstream), never dropped.
///
/// # Errors
/// The driver's.
pub async fn open_lots(conn: &mut Conn, symbol: &str) -> Result<Vec<Lot>, StorageError> {
    let cur = conn
        .execute(
            "SELECT symbol, qty, entry_ts, entry_price FROM trades \
             WHERE symbol=? AND exit_ts IS NULL ORDER BY COALESCE(entry_ts, 0)",
            &[symbol.into()],
        )
        .await?;
    let mut out = Vec::new();
    for r in &cur.rows {
        let acquired = match r.get("entry_ts").and_then(crate::storage::DbValue::as_f64) {
            Some(ts) => Some(
                local_from_ts(ts)
                    .ok_or_else(|| StorageError::Other(format!("entry_ts out of range: {ts}")))?
                    .date(),
            ),
            None => None,
        };
        #[allow(clippy::cast_possible_truncation)]
        let qty = match r.get("qty") {
            Some(crate::storage::DbValue::Int(i)) => *i,
            Some(crate::storage::DbValue::Float(f)) => f.trunc() as i64,
            _ => 0,
        };
        out.push(Lot {
            symbol: symbol.to_owned(),
            quantity: qty,
            acquired_on: acquired,
            price: r
                .get("entry_price")
                .and_then(crate::storage::DbValue::as_f64),
            corporate_action_adjusted: false,
        });
    }
    Ok(out)
}

/// `lots_from_rows(symbol, rows)`: lots from plain dicts (`quantity`, `acquired_on` as a date or
/// ISO string, `price`, `corporate_action_adjusted`).
///
/// # Errors
/// An `acquired_on` string that is not an ISO date (Python's `ValueError`).
pub fn lots_from_rows(symbol: &str, rows: &[PyDict]) -> Result<Vec<Lot>, String> {
    let mut out = Vec::new();
    for r in rows {
        let acquired = match r.get("acquired_on") {
            Some(PyValue::Str(s)) => Some(
                NaiveDate::parse_from_str(s, "%Y-%m-%d")
                    .map_err(|_| format!("Invalid isoformat string: {s:?}"))?,
            ),
            Some(PyValue::Date(d)) => Some(*d),
            _ => None,
        };
        let price = match r.get("price") {
            None | Some(PyValue::None) => None,
            v => Some(pyf(v)),
        };
        out.push(Lot {
            symbol: symbol.to_owned(),
            quantity: pyi(r.get("quantity")),
            acquired_on: acquired,
            price,
            corporate_action_adjusted: r
                .get("corporate_action_adjusted")
                .is_some_and(PyValue::truthy),
        });
    }
    Ok(out)
}

/// `review_sale(symbol, qty, current_price, lots, as_of=, review_days=)`: walk the exact proposed
/// sell quantity through its FIFO lots.
#[must_use]
#[allow(clippy::cast_precision_loss)]
pub fn review_sale(
    symbol: &str,
    qty: i64,
    current_price: f64,
    lots: &[Lot],
    as_of: NaiveDate,
    review_days: i64,
) -> TaxReview {
    let mut remaining = qty.max(0);
    let mut consumptions = Vec::new();
    let mut codes: BTreeSet<&'static str> = BTreeSet::new();
    let mut total_gain = 0.0;
    let mut gain_known = true;
    let mut flagged_qty = 0;

    let mut ordered: Vec<&Lot> = lots.iter().collect();
    ordered.sort_by_key(|l| l.acquired_on.unwrap_or(NaiveDate::MIN));
    for lot in ordered {
        if remaining <= 0 {
            break;
        }
        let take = remaining.min(lot.quantity.max(0));
        if take <= 0 {
            continue;
        }
        remaining -= take;
        let mut lot_codes: BTreeSet<&'static str> = BTreeSet::new();
        let (mut days_held, mut days_to_ltcg, mut gain, mut long_term) = (None, None, None, None);
        let mut flagged = false;
        match lot.acquired_on {
            None => {
                lot_codes.insert(TAX_MISSING_ACQUISITION_DATE);
                lot_codes.insert(TAX_DATA_UNKNOWN);
            }
            Some(d) => {
                let held = (as_of - d).num_days();
                days_held = Some(held);
                days_to_ltcg = Some(LONG_TERM_DAYS - held);
                long_term = Some(held >= LONG_TERM_DAYS);
            }
        }
        match lot.price {
            None => {
                lot_codes.insert(TAX_MISSING_ACQUISITION_PRICE);
                lot_codes.insert(TAX_DATA_UNKNOWN);
                gain_known = false;
            }
            Some(p) => {
                let g = (current_price - p) * take as f64;
                gain = Some(g);
                total_gain += g;
            }
        }
        if let (Some(g), Some(dl)) = (gain, days_to_ltcg) {
            if g <= 0.0 {
                lot_codes.insert(TAX_LOT_LOSS);
            } else if long_term == Some(true) {
                lot_codes.insert(TAX_LOT_LONG_TERM);
            } else if 0 < dl && dl <= review_days {
                lot_codes.insert(TAX_LOT_NEAR_LTCG);
                flagged = true;
                flagged_qty += take;
            }
        }
        codes.extend(lot_codes.iter().copied());
        consumptions.push(LotConsumption {
            lot: lot.clone(),
            quantity: take,
            days_held,
            days_to_ltcg,
            gain,
            long_term,
            flagged,
            codes: lot_codes.into_iter().collect(),
        });
    }
    let covered = qty - remaining;
    if remaining > 0 {
        codes.insert(TAX_LOTS_INCOMPLETE);
        codes.insert(TAX_DATA_UNKNOWN);
    }
    TaxReview {
        symbol: symbol.to_owned(),
        requested_qty: qty,
        covered_qty: covered,
        flagged: consumptions.iter().any(|c| c.flagged),
        data_unknown: codes.contains(TAX_DATA_UNKNOWN),
        estimated_gain: (gain_known && !consumptions.is_empty()).then(|| round_to(total_gain, 2)),
        consumptions,
        flagged_qty,
        codes: codes.into_iter().collect(),
    }
}

/// `review_sales(conn, proposed, as_of=, review_days=)`: `proposed` is `(symbol, (qty, price))`
/// in the caller's order; the result keeps it.
///
/// # Errors
/// The driver's.
pub async fn review_sales(
    conn: &mut Conn,
    proposed: &[(String, (i64, f64))],
    as_of: NaiveDate,
    review_days: i64,
) -> Result<Vec<(String, TaxReview)>, StorageError> {
    let mut out = Vec::new();
    for (sym, (qty, px)) in proposed {
        let lots = open_lots(conn, sym).await?;
        out.push((
            sym.clone(),
            review_sale(sym, *qty, *px, &lots, as_of, review_days),
        ));
    }
    Ok(out)
}

/// `flagged_symbols(reviews)`: sorted.
#[must_use]
pub fn flagged_symbols(reviews: &[(String, TaxReview)]) -> Vec<String> {
    let mut out: Vec<String> = reviews
        .iter()
        .filter(|(_, r)| r.flagged)
        .map(|(s, _)| s.clone())
        .collect();
    out.sort();
    out
}

/// `manual_review_items(reviews)`: each manual-review sale with its lot detail, by symbol.
#[must_use]
pub fn manual_review_items(reviews: &[(String, TaxReview)]) -> Vec<PyDict> {
    let mut sorted: Vec<&(String, TaxReview)> = reviews.iter().collect();
    sorted.sort_by(|a, b| a.0.cmp(&b.0));
    let mut out = Vec::new();
    for (_, r) in sorted {
        if !r.manual_review() {
            continue;
        }
        let mut item = r.as_dict();
        let detail = r
            .consumptions
            .iter()
            .map(|c| {
                PyValue::Map(dict([
                    ("quantity", PyValue::Int(c.quantity)),
                    (
                        "acquired_on",
                        opt(c.lot.acquired_on, |d| {
                            PyValue::Str(d.format("%Y-%m-%d").to_string())
                        }),
                    ),
                    ("acquired_at", opt(c.lot.price, PyValue::Float)),
                    (
                        "corporate_action_adjusted",
                        PyValue::Bool(c.lot.corporate_action_adjusted),
                    ),
                    ("days_held", opt(c.days_held, PyValue::Int)),
                    ("days_to_ltcg", opt(c.days_to_ltcg, PyValue::Int)),
                    ("gain", opt(c.gain, PyValue::Float)),
                    ("long_term", opt(c.long_term, PyValue::Bool)),
                    ("codes", codes_py(&c.codes)),
                ]))
            })
            .collect();
        item.insert("detail", PyValue::List(detail));
        out.push(item);
    }
    out
}

#[cfg(test)]
#[allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::float_cmp
)]
mod tests {
    use super::*;

    fn d(y: i32, m: u32, day: u32) -> NaiveDate {
        NaiveDate::from_ymd_opt(y, m, day).unwrap()
    }

    fn lot(q: i64, on: Option<NaiveDate>, price: Option<f64>) -> Lot {
        Lot {
            symbol: "X".into(),
            quantity: q,
            acquired_on: on,
            price,
            corporate_action_adjusted: false,
        }
    }

    #[test]
    fn a_profitable_lot_days_from_long_term_is_flagged() {
        // tests/test_tradebook.py: 329 days held, 36 short of long term, profitable.
        let r = review_sale(
            "X",
            100,
            100.0,
            &[lot(100, Some(d(2025, 9, 19)), Some(50.0))],
            d(2026, 8, 14),
            DEFAULT_REVIEW_DAYS,
        );
        assert!(!r.data_unknown);
        assert_eq!(r.estimated_gain, Some(5000.0));
        assert!(r.codes.contains(&TAX_LOT_NEAR_LTCG) && r.flagged);
    }

    #[test]
    fn an_older_lot_is_long_term_and_not_flagged() {
        let r = review_sale(
            "X",
            100,
            100.0,
            &[lot(100, Some(d(2024, 1, 5)), Some(50.0))],
            d(2026, 8, 14),
            DEFAULT_REVIEW_DAYS,
        );
        assert!(r.codes.contains(&TAX_LOT_LONG_TERM) && !r.flagged);
    }

    #[test]
    fn missing_date_and_price_are_unknown_never_safe() {
        let r = review_sale(
            "X",
            10,
            100.0,
            &[lot(5, None, Some(1.0)), lot(5, Some(d(2026, 1, 1)), None)],
            d(2026, 8, 14),
            45,
        );
        assert!(r.data_unknown && r.manual_review());
        assert_eq!(r.estimated_gain, None);
        assert!(r.codes.contains(&TAX_MISSING_ACQUISITION_DATE));
        assert!(r.codes.contains(&TAX_MISSING_ACQUISITION_PRICE));
    }

    #[test]
    fn a_sale_beyond_the_recorded_lots_is_incomplete() {
        let r = review_sale(
            "X",
            50,
            100.0,
            &[lot(10, Some(d(2026, 1, 1)), Some(90.0))],
            d(2026, 8, 14),
            45,
        );
        assert_eq!(r.covered_qty, 10);
        assert!(r.codes.contains(&TAX_LOTS_INCOMPLETE) && r.data_unknown);
    }

    #[test]
    fn fifo_takes_the_oldest_lot_first_and_a_loser_is_never_deferred() {
        let lots = [
            lot(10, Some(d(2025, 9, 1)), Some(200.0)),
            lot(10, Some(d(2025, 1, 1)), Some(50.0)),
        ];
        let r = review_sale("X", 10, 100.0, &lots, d(2026, 8, 14), 45);
        assert_eq!(r.consumptions.len(), 1);
        assert_eq!(r.consumptions[0].lot.acquired_on, Some(d(2025, 1, 1)));
        let loss = review_sale("X", 20, 100.0, &lots, d(2026, 8, 14), 45);
        assert!(
            loss.consumptions[1].codes.contains(&TAX_LOT_LOSS) && !loss.consumptions[1].flagged
        );
    }

    #[test]
    fn manual_review_items_carry_the_acquisition_price() {
        let r = review_sale(
            "X",
            100,
            100.0,
            &[lot(100, Some(d(2025, 9, 19)), Some(50.0))],
            d(2026, 8, 14),
            45,
        );
        let items = manual_review_items(&[("X".into(), r)]);
        let PyValue::List(detail) = items[0].get("detail").unwrap() else {
            panic!()
        };
        let PyValue::Map(first) = &detail[0] else {
            panic!()
        };
        assert_eq!(first.get("acquired_at"), Some(&PyValue::Float(50.0)));
        assert_eq!(
            flagged_symbols(&[("X".into(), review_sale("X", 1, 1.0, &[], d(2026, 1, 1), 45))]),
            Vec::<String>::new()
        );
    }
}
