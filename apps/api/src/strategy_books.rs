//! STRATEGY-BOOKS (owner, 12 Nov 2026): every live open line of an equity strategy sleeve — an
//! automated buy or a Buy-ticket click — filed into that strategy's own `MY_STRATEGY` portfolio.
//!
//! One book per user per strategy, found by `portfolio.strategy_key` (migration 0015). Holdings
//! mirror `quantity_open` / `entry_avg` / `entry_date`; a share moved into a book leaves the
//! broker's pile (the "not in any portfolio" group) so it is never counted twice. Database only;
//! nothing here reaches a broker (law 2).

use std::collections::{BTreeMap, BTreeSet};

use chrono::NaiveDate;
use rust_decimal::{Decimal, RoundingStrategy};
use sqlx::{PgConnection, Row};

/// A strategy that owns a book.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Strategy {
    /// `tw_position`, and Buy-ticket lines from the Tight page.
    Twt,
    /// `sw_position` (Swing, Overlap and Opportunities tickets).
    Swing,
    /// `vb_position`, and Buy-ticket lines from the Volume page.
    Vbt,
}

impl Strategy {
    /// All three, in a stable order.
    pub const ALL: [Self; 3] = [Self::Twt, Self::Swing, Self::Vbt];

    /// `portfolio.strategy_key`.
    #[must_use]
    pub const fn key(self) -> &'static str {
        match self {
            Self::Twt => "twt",
            Self::Swing => "swing",
            Self::Vbt => "vbt",
        }
    }

    /// The book's name when it is created.
    #[must_use]
    pub const fn book_name(self) -> &'static str {
        match self {
            Self::Twt => "Three-weeks-tight",
            Self::Swing => "Swing",
            Self::Vbt => "Volume breakout",
        }
    }
}

/// A Buy-ticket line lives in `sw_position`; its page is the `src=` token of its plan-line note
/// (`OPPS ... src=twt ...`). Overlap, Opportunities, Swing and no note stay with Swing, whose
/// monitor manages the line.
#[must_use]
pub fn strategy_for_swing_note(note: Option<&str>) -> Strategy {
    let src = note
        .and_then(|n| n.split_whitespace().find_map(|t| t.strip_prefix("src=")))
        .map(str::to_ascii_lowercase);
    match src.as_deref() {
        Some("twt") => Strategy::Twt,
        Some("vbt") => Strategy::Vbt,
        _ => Strategy::Swing,
    }
}

/// `(instrument_id, broker_account_id)` — `portfolio_holding`'s key within one portfolio.
pub type Key = (i64, i64);

/// One live open line of a sleeve.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OpenLine {
    /// The book it belongs in.
    pub strategy: Strategy,
    /// `instrument_id`.
    pub instrument_id: i64,
    /// `broker_account_id` (the user's default account when the line has none).
    pub broker_account_id: i64,
    /// `quantity_open`.
    pub quantity: Decimal,
    /// `entry_avg`.
    pub entry_avg: Decimal,
    /// `entry_date`.
    pub entry_date: NaiveDate,
}

/// What a book row should be.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Target {
    /// Sum of `quantity_open`.
    pub quantity: Decimal,
    /// Quantity-weighted `entry_avg`, 4 dp (`numeric(18,4)`).
    pub avg_price: Decimal,
    /// The earliest `entry_date`.
    pub entry_date: NaiveDate,
}

/// A book row as stored.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BookRow {
    /// `quantity`.
    pub quantity: Option<Decimal>,
    /// `avg_price`.
    pub avg_price: Option<Decimal>,
    /// `first_bought_on`.
    pub first_bought_on: Option<NaiveDate>,
}

/// One write to a book.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Change {
    /// A new row: `added_on = first_bought_on = entry_date`.
    Insert(Key, Target),
    /// Quantity / price moved (a partial exit, an add). `added_on` is never rewritten, and
    /// `first_bought_on` is only filled when empty, so a T+1 rollover books no flow.
    Update {
        /// The row.
        key: Key,
        /// The new quantity.
        quantity: Decimal,
        /// The new average.
        avg_price: Decimal,
        /// `Some` only when the stored `first_bought_on` was empty.
        first_bought_on: Option<NaiveDate>,
        /// Shares added since the last sync (taken from the pile), zero on a reduction.
        increase: Decimal,
    },
    /// The line closed.
    Delete(Key),
}

fn q4(d: Decimal) -> Decimal {
    d.round_dp_with_strategy(4, RoundingStrategy::MidpointNearestEven)
}

/// Aggregate one strategy's lines into rows (several lines in one instrument become one row).
#[must_use]
pub fn targets(lines: &[OpenLine], strategy: Strategy) -> BTreeMap<Key, Target> {
    let mut acc: BTreeMap<Key, (Decimal, Decimal, NaiveDate)> = BTreeMap::new();
    for l in lines
        .iter()
        .filter(|l| l.strategy == strategy && l.quantity > Decimal::ZERO)
    {
        let e = acc
            .entry((l.instrument_id, l.broker_account_id))
            .or_insert((Decimal::ZERO, Decimal::ZERO, l.entry_date));
        e.0 += l.quantity;
        e.1 += l.quantity * l.entry_avg;
        e.2 = e.2.min(l.entry_date);
    }
    acc.into_iter()
        .map(|(k, (q, cost, d))| {
            (
                k,
                Target {
                    quantity: q,
                    avg_price: q4(cost / q),
                    entry_date: d,
                },
            )
        })
        .collect()
}

/// The writes that make `current` equal `wanted`; empty when nothing moved (idempotent).
#[must_use]
pub fn plan(current: &BTreeMap<Key, BookRow>, wanted: &BTreeMap<Key, Target>) -> Vec<Change> {
    let keys: BTreeSet<Key> = current.keys().chain(wanted.keys()).copied().collect();
    let mut out = Vec::new();
    for key in keys {
        match (current.get(&key), wanted.get(&key)) {
            (Some(_), None) => out.push(Change::Delete(key)),
            (None, Some(t)) => out.push(Change::Insert(key, t.clone())),
            (Some(row), Some(t)) => {
                let fill = row.first_bought_on.is_none().then_some(t.entry_date);
                let have = row.quantity.unwrap_or(Decimal::ZERO);
                if row.quantity != Some(t.quantity)
                    || row.avg_price != Some(t.avg_price)
                    || fill.is_some()
                {
                    let increase = (t.quantity - have).max(Decimal::ZERO);
                    out.push(Change::Update {
                        key,
                        quantity: t.quantity,
                        avg_price: t.avg_price,
                        first_bought_on: fill,
                        increase,
                    });
                }
            }
            (None, None) => {}
        }
    }
    out
}

/// What a sync did, per user.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SyncReport {
    /// Books created.
    pub created: usize,
    /// Rows inserted, updated, deleted.
    pub inserted: usize,
    /// Rows updated.
    pub updated: usize,
    /// Rows deleted.
    pub deleted: usize,
    /// Shares taken out of the broker pile.
    pub from_pile: Decimal,
}

impl SyncReport {
    fn absorb(&mut self, other: &Self) {
        self.created += other.created;
        self.inserted += other.inserted;
        self.updated += other.updated;
        self.deleted += other.deleted;
        self.from_pile += other.from_pile;
    }
}

const DEFAULT_ACCOUNT: &str = "(SELECT a.id FROM broker_account a WHERE a.user_id = p.user_id \
     ORDER BY (a.broker_id <> 'zerodha'), a.id LIMIT 1)";

/// Every non-simulated open line of the user's three equity sleeves.
///
/// # Errors
/// The database's.
pub async fn open_lines(
    conn: &mut PgConnection,
    user_id: i64,
) -> Result<Vec<OpenLine>, sqlx::Error> {
    let mut out = Vec::new();
    for (strategy, table, states) in [
        (Strategy::Twt, "tw_position", "('OPEN')"),
        (Strategy::Vbt, "vb_position", "('OPEN')"),
    ] {
        let sql = format!(
            "SELECT p.instrument_id, COALESCE(p.broker_account_id, {DEFAULT_ACCOUNT}) AS account, \
             p.quantity_open::numeric AS quantity, p.entry_avg, p.entry_date FROM {table} p \
             WHERE p.user_id = $1 AND p.simulated IS false AND p.state IN {states} \
             AND p.quantity_open > 0"
        );
        for row in sqlx::query(&sql)
            .bind(user_id)
            .fetch_all(&mut *conn)
            .await?
        {
            if let Some(line) = line_from(&row, strategy)? {
                out.push(line);
            }
        }
    }
    let sql = format!(
        "SELECT p.instrument_id, COALESCE(p.broker_account_id, {DEFAULT_ACCOUNT}) AS account, \
         p.quantity_open::numeric AS quantity, p.entry_avg, p.entry_date, \
         (SELECT l.note FROM sw_plan_line l WHERE l.position_id = p.id \
          AND l.note LIKE 'OPPS %' ORDER BY l.id DESC LIMIT 1) AS note \
         FROM sw_position p WHERE p.user_id = $1 AND p.simulated IS false \
         AND p.state IN ('OPEN', 'PARTIAL') AND p.quantity_open > 0"
    );
    for row in sqlx::query(&sql)
        .bind(user_id)
        .fetch_all(&mut *conn)
        .await?
    {
        let note: Option<String> = row.try_get("note")?;
        if let Some(line) = line_from(&row, strategy_for_swing_note(note.as_deref()))? {
            out.push(line);
        }
    }
    Ok(out)
}

fn line_from(
    row: &sqlx::postgres::PgRow,
    strategy: Strategy,
) -> Result<Option<OpenLine>, sqlx::Error> {
    let account: Option<i64> = row.try_get("account")?;
    let Some(broker_account_id) = account else {
        return Ok(None);
    };
    Ok(Some(OpenLine {
        strategy,
        instrument_id: row.try_get("instrument_id")?,
        broker_account_id,
        quantity: row.try_get("quantity")?,
        entry_avg: row.try_get("entry_avg")?,
        entry_date: row.try_get("entry_date")?,
    }))
}

/// The book's id: by key, else an unkeyed `MY_STRATEGY` portfolio of the same name (the old
/// holdings-sync Swing book) is adopted, else one is created when there is something to file.
async fn book_id(
    conn: &mut PgConnection,
    user_id: i64,
    strategy: Strategy,
    started_on: Option<NaiveDate>,
) -> Result<(Option<i64>, bool), sqlx::Error> {
    let keyed: Option<i64> =
        sqlx::query_scalar("SELECT id FROM portfolio WHERE user_id = $1 AND strategy_key = $2")
            .bind(user_id)
            .bind(strategy.key())
            .fetch_optional(&mut *conn)
            .await?;
    if keyed.is_some() {
        return Ok((keyed, false));
    }
    let Some(started_on) = started_on else {
        return Ok((None, false));
    };
    let adopted: Option<i64> = sqlx::query_scalar(
        "UPDATE portfolio SET strategy_key = $2 WHERE id = (SELECT id FROM portfolio \
         WHERE user_id = $1 AND source = 'MY_STRATEGY' AND name = $3 AND strategy_key IS NULL \
         ORDER BY id LIMIT 1) RETURNING id",
    )
    .bind(user_id)
    .bind(strategy.key())
    .bind(strategy.book_name())
    .fetch_optional(&mut *conn)
    .await?;
    if adopted.is_some() {
        return Ok((adopted, false));
    }
    let id: i64 = sqlx::query_scalar(
        "INSERT INTO portfolio (user_id, name, kind, source, started_on, strategy_key) \
         VALUES ($1, $2, 'CAPITAL', 'MY_STRATEGY', $3, $4) RETURNING id",
    )
    .bind(user_id)
    .bind(strategy.book_name())
    .bind(started_on)
    .bind(strategy.key())
    .fetch_one(&mut *conn)
    .await?;
    Ok((Some(id), true))
}

/// Move up to `shares` of an instrument out of the account's broker pile (never below zero).
async fn take_from_pile(
    conn: &mut PgConnection,
    user_id: i64,
    (instrument_id, broker_account_id): Key,
    shares: Decimal,
) -> Result<Decimal, sqlx::Error> {
    if shares <= Decimal::ZERO {
        return Ok(Decimal::ZERO);
    }
    let pile: Option<(i64, Option<Decimal>)> = sqlx::query_as(
        "SELECT h.portfolio_id, h.quantity FROM portfolio_holding h \
         JOIN portfolio p ON p.id = h.portfolio_id \
         WHERE p.user_id = $1 AND p.broker_account_id = $2 AND p.is_broker_pile IS true \
         AND h.instrument_id = $3 AND h.broker_account_id = $2",
    )
    .bind(user_id)
    .bind(broker_account_id)
    .bind(instrument_id)
    .fetch_optional(&mut *conn)
    .await?;
    let Some((pile_id, Some(held))) = pile else {
        return Ok(Decimal::ZERO);
    };
    let taken = shares.min(held).max(Decimal::ZERO);
    if taken >= held {
        sqlx::query(
            "DELETE FROM portfolio_holding WHERE portfolio_id = $1 AND instrument_id = $2 \
             AND broker_account_id = $3",
        )
        .bind(pile_id)
        .bind(instrument_id)
        .bind(broker_account_id)
        .execute(&mut *conn)
        .await?;
    } else if taken > Decimal::ZERO {
        sqlx::query(
            "UPDATE portfolio_holding SET quantity = $1 WHERE portfolio_id = $2 \
             AND instrument_id = $3 AND broker_account_id = $4",
        )
        .bind(held - taken)
        .bind(pile_id)
        .bind(instrument_id)
        .bind(broker_account_id)
        .execute(&mut *conn)
        .await?;
    }
    Ok(taken)
}

/// Sync one user's three books.
///
/// # Errors
/// The database's.
pub async fn sync_user(conn: &mut PgConnection, user_id: i64) -> Result<SyncReport, sqlx::Error> {
    let lines = open_lines(conn, user_id).await?;
    let mut report = SyncReport::default();
    for strategy in Strategy::ALL {
        let wanted = targets(&lines, strategy);
        let started = wanted.values().map(|t| t.entry_date).min();
        let (book, created) = book_id(conn, user_id, strategy, started).await?;
        let Some(book) = book else {
            continue;
        };
        report.created += usize::from(created);
        let mut current = BTreeMap::new();
        for row in sqlx::query(
            "SELECT instrument_id, broker_account_id, quantity, avg_price, first_bought_on \
             FROM portfolio_holding WHERE portfolio_id = $1",
        )
        .bind(book)
        .fetch_all(&mut *conn)
        .await?
        {
            current.insert(
                (
                    row.try_get("instrument_id")?,
                    row.try_get("broker_account_id")?,
                ),
                BookRow {
                    quantity: row.try_get("quantity")?,
                    avg_price: row.try_get("avg_price")?,
                    first_bought_on: row.try_get("first_bought_on")?,
                },
            );
        }
        for change in plan(&current, &wanted) {
            match change {
                Change::Insert(key, t) => {
                    // MANUAL: the sleeve's entry date is authoritative; trade capture's
                    // instrument-wide re-dating (NONE/BROKER rows only) leaves it alone.
                    sqlx::query(
                        "INSERT INTO portfolio_holding (portfolio_id, instrument_id, \
                         broker_account_id, portfolio_kind, quantity, avg_price, added_on, \
                         first_bought_on, history_source) \
                         VALUES ($1, $2, $3, 'CAPITAL', $4, $5, $6, $6, 'MANUAL')",
                    )
                    .bind(book)
                    .bind(key.0)
                    .bind(key.1)
                    .bind(t.quantity)
                    .bind(t.avg_price)
                    .bind(t.entry_date)
                    .execute(&mut *conn)
                    .await?;
                    report.inserted += 1;
                    report.from_pile += take_from_pile(conn, user_id, key, t.quantity).await?;
                }
                Change::Update {
                    key,
                    quantity,
                    avg_price,
                    first_bought_on,
                    increase,
                } => {
                    sqlx::query(
                        "UPDATE portfolio_holding SET quantity = $1, avg_price = $2, \
                         first_bought_on = COALESCE(first_bought_on, $3) \
                         WHERE portfolio_id = $4 AND instrument_id = $5 AND broker_account_id = $6",
                    )
                    .bind(quantity)
                    .bind(avg_price)
                    .bind(first_bought_on)
                    .bind(book)
                    .bind(key.0)
                    .bind(key.1)
                    .execute(&mut *conn)
                    .await?;
                    report.updated += 1;
                    report.from_pile += take_from_pile(conn, user_id, key, increase).await?;
                }
                Change::Delete(key) => {
                    sqlx::query(
                        "DELETE FROM portfolio_holding WHERE portfolio_id = $1 \
                         AND instrument_id = $2 AND broker_account_id = $3",
                    )
                    .bind(book)
                    .bind(key.0)
                    .bind(key.1)
                    .execute(&mut *conn)
                    .await?;
                    report.deleted += 1;
                }
            }
        }
    }
    Ok(report)
}

/// Every user with a sleeve position or a book.
///
/// # Errors
/// The database's.
pub async fn sync_all(conn: &mut PgConnection) -> Result<(usize, SyncReport), sqlx::Error> {
    let users: Vec<i64> = sqlx::query_scalar(
        "SELECT user_id FROM tw_position UNION SELECT user_id FROM sw_position \
         UNION SELECT user_id FROM vb_position \
         UNION SELECT user_id FROM portfolio WHERE strategy_key IS NOT NULL ORDER BY 1",
    )
    .fetch_all(&mut *conn)
    .await?;
    let mut total = SyncReport::default();
    for user in &users {
        total.absorb(&sync_user(conn, *user).await?);
    }
    Ok((users.len(), total))
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    fn d(s: &str) -> Decimal {
        s.parse().unwrap()
    }
    fn day(n: u32) -> NaiveDate {
        NaiveDate::from_ymd_opt(2026, 11, n).unwrap()
    }
    fn line(strategy: Strategy, inst: i64, q: &str, avg: &str, on: u32) -> OpenLine {
        OpenLine {
            strategy,
            instrument_id: inst,
            broker_account_id: 1,
            quantity: d(q),
            entry_avg: d(avg),
            entry_date: day(on),
        }
    }
    fn stored(t: &Target) -> BookRow {
        BookRow {
            quantity: Some(t.quantity),
            avg_price: Some(t.avg_price),
            first_bought_on: Some(t.entry_date),
        }
    }

    #[test]
    fn ticket_source_picks_the_book() {
        let n = |s| Some(s);
        assert_eq!(
            strategy_for_swing_note(n("OPPS product=CNC src=twt setup=FLAG")),
            Strategy::Twt
        );
        assert_eq!(strategy_for_swing_note(n("OPPS src=vbt")), Strategy::Vbt);
        assert_eq!(
            strategy_for_swing_note(n("OPPS src=swing")),
            Strategy::Swing
        );
        assert_eq!(
            strategy_for_swing_note(n("OPPS src=overlap")),
            Strategy::Swing
        );
        assert_eq!(
            strategy_for_swing_note(n("OPPS src=opportunities")),
            Strategy::Swing
        );
        assert_eq!(strategy_for_swing_note(None), Strategy::Swing);
    }

    #[test]
    fn an_open_line_becomes_a_row_dated_from_entry() {
        let lines = [line(Strategy::Twt, 7, "340", "366.4000", 9)];
        let want = targets(&lines, Strategy::Twt);
        let changes = plan(&BTreeMap::new(), &want);
        assert_eq!(
            changes,
            vec![Change::Insert(
                (7, 1),
                Target {
                    quantity: d("340"),
                    avg_price: d("366.4000"),
                    entry_date: day(9)
                }
            )]
        );
        assert!(targets(&lines, Strategy::Swing).is_empty());
    }

    #[test]
    fn a_partial_exit_reduces_and_takes_nothing_from_the_pile() {
        let before = targets(
            &[line(Strategy::Swing, 2, "300", "100", 3)],
            Strategy::Swing,
        );
        let current: BTreeMap<_, _> = before.iter().map(|(k, t)| (*k, stored(t))).collect();
        let after = targets(
            &[line(Strategy::Swing, 2, "150", "100", 3)],
            Strategy::Swing,
        );
        assert_eq!(
            plan(&current, &after),
            vec![Change::Update {
                key: (2, 1),
                quantity: d("150"),
                avg_price: d("100.0000"),
                first_bought_on: None,
                increase: Decimal::ZERO,
            }]
        );
    }

    #[test]
    fn a_closed_line_is_removed() {
        let before = targets(&[line(Strategy::Vbt, 4, "10", "50", 3)], Strategy::Vbt);
        let current: BTreeMap<_, _> = before.iter().map(|(k, t)| (*k, stored(t))).collect();
        assert_eq!(
            plan(&current, &BTreeMap::new()),
            vec![Change::Delete((4, 1))]
        );
    }

    #[test]
    fn rerun_and_t1_rollover_write_nothing() {
        let want = targets(
            &[line(Strategy::Twt, 9, "204", "610.7669", 9)],
            Strategy::Twt,
        );
        let current: BTreeMap<_, _> = want.iter().map(|(k, t)| (*k, stored(t))).collect();
        // Day 2: the same line, now a broker holding; the book row is untouched.
        assert!(plan(&current, &want).is_empty());
    }

    #[test]
    fn two_lines_in_one_name_are_one_row_weighted() {
        let lines = [
            line(Strategy::Swing, 5, "100", "10", 4),
            line(Strategy::Swing, 5, "300", "14", 6),
        ];
        let t = &targets(&lines, Strategy::Swing)[&(5, 1)];
        assert_eq!(t.quantity, d("400"));
        assert_eq!(t.avg_price, d("13.0000"));
        assert_eq!(t.entry_date, day(4));
    }

    #[test]
    fn an_adopted_row_without_a_buy_date_gets_one_and_its_add_comes_from_the_pile() {
        let want = targets(
            &[line(Strategy::Swing, 3, "380", "788.6", 12)],
            Strategy::Swing,
        );
        let current = BTreeMap::from([(
            (3, 1),
            BookRow {
                quantity: Some(d("300")),
                avg_price: None,
                first_bought_on: None,
            },
        )]);
        assert_eq!(
            plan(&current, &want),
            vec![Change::Update {
                key: (3, 1),
                quantity: d("380"),
                avg_price: d("788.6000"),
                first_bought_on: Some(day(12)),
                increase: d("80"),
            }]
        );
    }
}
