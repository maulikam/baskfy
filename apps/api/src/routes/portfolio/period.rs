//! Period P&L (Today / This week / Last 30 days) counted from the day a holding was bought
//! (owner, 12 Nov 2026: "If I invest today ... how could the profit and loss be weeks?").
//! Pure: no I/O, no clock.

use chrono::NaiveDate;

use super::pydec::Dec;

/// Where a holding's period base came from.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Basis {
    /// Held before the period began: the close at the period start.
    Held,
    /// Bought inside the period: its average cost.
    Bought,
    /// No purchase date (or bought in the period with no cost on record): the period-start
    /// close, as before — flagged, not guessed.
    Undated,
}

/// One holding's move over the period.
#[derive(Debug, Clone)]
pub(crate) struct Leg {
    pub(crate) amount: Dec,
    pub(crate) base: Dec,
    pub(crate) basis: Basis,
}

/// Inputs for one holding.
pub(crate) struct LegInput<'a> {
    pub(crate) quantity: &'a Dec,
    pub(crate) price_now: &'a Dec,
    /// The close the period is measured from; `None` when the instrument has none.
    pub(crate) start_close: Option<&'a Dec>,
    /// The session of that close; `None` when there is no period start.
    pub(crate) since: Option<NaiveDate>,
    pub(crate) acquired_on: Option<NaiveDate>,
    pub(crate) avg_price: Option<&'a Dec>,
}

/// The base is the later of the period start and the purchase: bought after `since` → its
/// average cost; held at `since` → the close then; no date → the close then, flagged.
pub(crate) fn period_leg(input: &LegInput<'_>) -> Option<Leg> {
    let bought_in_period = input
        .acquired_on
        .is_some_and(|on| input.since.is_none_or(|since| on > since));
    let (price, basis) = if bought_in_period {
        match input.avg_price.filter(|p| !p.is_zero()) {
            Some(cost) => (cost, Basis::Bought),
            None => (input.start_close?, Basis::Undated),
        }
    } else if input.acquired_on.is_some() {
        (input.start_close?, Basis::Held)
    } else {
        (input.start_close?, Basis::Undated)
    };
    Some(Leg {
        amount: input.quantity.mul(&input.price_now.sub(price)),
        base: input.quantity.mul(price),
        basis,
    })
}

/// The sum of a set of legs.
#[derive(Debug, Clone, Default)]
pub(crate) struct PeriodTotal {
    pub(crate) amount: Option<Dec>,
    pub(crate) base: Option<Dec>,
    pub(crate) undated: usize,
}

pub(crate) fn sum_legs(legs: impl IntoIterator<Item = Leg>) -> PeriodTotal {
    let mut total = PeriodTotal::default();
    for leg in legs {
        let zero = Dec::lit("0");
        total.amount = Some(
            total
                .amount
                .unwrap_or_else(|| zero.clone())
                .add(&leg.amount),
        );
        total.base = Some(total.base.unwrap_or(zero).add(&leg.base));
        if leg.basis == Basis::Undated {
            total.undated += 1;
        }
    }
    total
}

/// The footnote for holdings measured the old way.
pub(crate) fn undated_note(undated: usize) -> Option<String> {
    match undated {
        0 => None,
        1 => Some(
            "1 holding has no purchase date or price on record, so it is measured from the \
             period's opening close"
                .to_owned(),
        ),
        n => Some(format!(
            "{n} holdings have no purchase date or price on record, so they are measured from \
             the period's opening close"
        )),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn d(day: u32) -> NaiveDate {
        NaiveDate::from_ymd_opt(2026, 11, day).unwrap_or_default()
    }

    fn leg(acquired: Option<NaiveDate>, avg: Option<&str>, since: NaiveDate) -> Option<Leg> {
        let q = Dec::lit("10");
        let now = Dec::lit("110");
        let start = Dec::lit("90");
        let avg = avg.map(Dec::lit);
        period_leg(&LegInput {
            quantity: &q,
            price_now: &now,
            start_close: Some(&start),
            since: Some(since),
            acquired_on: acquired,
            avg_price: avg.as_ref(),
        })
    }

    fn s(v: &Dec) -> String {
        v.to_sci_string()
    }

    #[test]
    fn bought_today_counts_only_from_the_buy_price() {
        // Previous close on 11 Nov; bought on 12 Nov at 105.
        let l = leg(Some(d(12)), Some("105"), d(11)).unwrap_or_else(|| unreachable!());
        assert_eq!(s(&l.amount), "50");
        assert_eq!(s(&l.base), "1050");
        assert_eq!(l.basis, Basis::Bought);
    }

    #[test]
    fn bought_mid_week_is_measured_from_cost_not_last_weeks_close() {
        // Week measured from Friday 6 Nov's close; bought Tuesday 10 Nov at 100.
        let l = leg(Some(d(10)), Some("100"), d(6)).unwrap_or_else(|| unreachable!());
        assert_eq!(s(&l.amount), "100");
        assert_eq!(s(&l.base), "1000");
        assert_eq!(l.basis, Basis::Bought);
    }

    #[test]
    fn held_all_period_is_measured_from_the_period_start_close() {
        let l = leg(Some(d(1)), Some("50"), d(6)).unwrap_or_else(|| unreachable!());
        assert_eq!(s(&l.amount), "200");
        assert_eq!(s(&l.base), "900");
        assert_eq!(l.basis, Basis::Held);
        // Bought on the start session itself: its close is already at risk.
        let l = leg(Some(d(6)), Some("50"), d(6)).unwrap_or_else(|| unreachable!());
        assert_eq!(l.basis, Basis::Held);
    }

    #[test]
    fn no_acquisition_date_keeps_the_old_rule_and_is_flagged() {
        let l = leg(None, Some("50"), d(6)).unwrap_or_else(|| unreachable!());
        assert_eq!(s(&l.amount), "200");
        assert_eq!(l.basis, Basis::Undated);
        let bought_no_cost = leg(Some(d(10)), None, d(6)).unwrap_or_else(|| unreachable!());
        assert_eq!(bought_no_cost.basis, Basis::Undated);
        let total = sum_legs([l, bought_no_cost]);
        assert_eq!(total.undated, 2);
        assert!(undated_note(total.undated).is_some_and(|n| n.starts_with("2 holdings")));
    }

    #[test]
    fn a_new_listing_bought_today_needs_no_previous_close() {
        let q = Dec::lit("2");
        let now = Dec::lit("120");
        let avg = Dec::lit("100");
        let l = period_leg(&LegInput {
            quantity: &q,
            price_now: &now,
            start_close: None,
            since: None,
            acquired_on: Some(d(12)),
            avg_price: Some(&avg),
        })
        .unwrap_or_else(|| unreachable!());
        assert_eq!(s(&l.amount), "40");
    }

    #[test]
    fn percent_is_over_the_base_actually_at_risk() {
        let held = leg(Some(d(1)), None, d(6)).unwrap_or_else(|| unreachable!());
        let bought = leg(Some(d(10)), Some("100"), d(6)).unwrap_or_else(|| unreachable!());
        let total = sum_legs([held, bought]);
        let (amount, base) = (
            total.amount.unwrap_or_else(Dec::zero),
            total.base.unwrap_or_else(Dec::zero),
        );
        assert_eq!(s(&amount), "300");
        assert_eq!(s(&base), "1900");
    }
}
