//! Ports `app.options_ledger` (OP11): "every close writes the record, and the record governs
//! tomorrow". The journal row from the session's own fills, then `04` §9.1 for the sleeve and
//! §9.3 for the book; a pause is written (never shortened) and audited, and a `close_now` pause
//! raises an exit plan for every open position it covers. Nothing here places an order.

use chrono::{DateTime, Datelike, Duration, FixedOffset, NaiveDate, Weekday};
use rust_decimal::Decimal;

use super::core::{
    BookCeilings, Mode, Pause, RealisedTrade, RiskConfig, SLEEVES, Side, Sleeve, Structure,
    book_limits, evaluate_book, evaluate_sleeve, group_of, group_str, pnl_points, side_of,
    sleeve_str,
};
use super::py::{self, col, int, text};
use super::store::{ExitVerdict, PgOptionsStore, StoreError};
use crate::storage::{DbValue, Param};
use crate::wire::{PyDict, PyValue};

fn d(value: &DbValue) -> Decimal {
    py::dec(value).unwrap_or(Decimal::ZERO)
}

fn day(value: &DbValue) -> Result<NaiveDate, StoreError> {
    py::opt_day(value)
        .ok_or_else(|| StoreError::Data(format!("TypeError: not a date: {}", text(value))))
}

/// The journal figures `after_close` needs back (`figures.r_inr`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Written {
    /// R.
    pub r_inr: Decimal,
}

/// `write_journal(store, session_id, reason=)`: one `op_journal` row for a closed session.
///
/// # Errors
/// The database.
pub async fn write_journal(
    store: &mut PgOptionsStore<'_>,
    session_id: i64,
    reason: &str,
    now: DateTime<FixedOffset>,
) -> Result<Option<Written>, StoreError> {
    let sql = format!(
        "SELECT s.sleeve, s.trade_date, s.expiry_used, p.plan_id, p.structure, p.sizing_mode, \
         p.risk_budget_inr, p.risk_per_lot_inr, p.lots, p.lot_size, p.detail, \
         pos.opened_at, pos.closed_at, pos.peak_value, pos.trough_value, pos.simulated \
         FROM {} s JOIN {} p ON p.plan_id = s.plan_id \
         JOIN {} pos ON pos.session_id = s.id \
         WHERE s.id = ? AND s.user_id = ?",
        store.t("op_session"),
        store.t("op_plan"),
        store.t("op_position")
    );
    let head = store
        .conn
        .execute(&sql, &[Param::Int(session_id), Param::Int(store.user_id)])
        .await?
        .rows
        .into_iter()
        .next();
    let Some(head) = head else { return Ok(None) };
    let sql = format!(
        "SELECT f.price, f.quantity, o.side, (o.side <> l.side) AS closing \
         FROM {} f JOIN {} o ON o.id = f.order_id \
         JOIN {} l ON l.id = f.leg_id JOIN {} p ON p.id = l.plan_id \
         WHERE p.plan_id = ? ORDER BY f.id",
        store.t("op_fill"),
        store.t("op_order"),
        store.t("op_leg"),
        store.t("op_plan")
    );
    let rows = store
        .conn
        .execute(&sql, &[Param::Text(text(col(&head, "plan_id")))])
        .await?
        .rows;
    if rows.is_empty() {
        return Ok(None);
    }
    let mut fills = Vec::with_capacity(rows.len());
    for r in &rows {
        let s = text(col(r, "side"));
        fills.push(baskfy_core::options::ledger::LegFill {
            side: side_of(&s).ok_or_else(|| {
                StoreError::Data(format!("ValueError: '{s}' is not a valid Side"))
            })?,
            price: d(col(r, "price")),
            quantity: int(col(r, "quantity")),
            closing: py::truthy(col(r, "closing")),
        });
    }
    let paper_one_lot = text(col(&head, "sizing_mode")) == "PAPER_ONE_LOT";
    let mut r_inr = d(col(
        &head,
        if paper_one_lot {
            "risk_per_lot_inr"
        } else {
            "risk_budget_inr"
        },
    ));
    if r_inr <= Decimal::ZERO {
        let budget = d(col(&head, "risk_budget_inr"));
        r_inr = if budget.is_zero() {
            d(col(&head, "risk_per_lot_inr"))
        } else {
            budget
        };
    }
    if r_inr <= Decimal::ZERO {
        return Ok(None);
    }
    let closed = if py::truthy(col(&head, "closed_at")) {
        py::aware(col(&head, "closed_at")).unwrap_or(now)
    } else {
        now
    };
    let opened = py::aware(col(&head, "opened_at"))
        .ok_or_else(|| StoreError::Data("AssertionError".to_owned()))?;
    let session = baskfy_core::options::ledger::JournalSession {
        r_inr,
        quantity: int(col(&head, "lots")) * int(col(&head, "lot_size")),
        opened_at: opened,
        closed_at: closed,
        peak_points: py::dec(col(&head, "peak_value")),
        trough_points: py::dec(col(&head, "trough_value")),
    };
    let figures = baskfy_core::options::ledger::journal_figures(
        &fills,
        &session,
        &baskfy_core::options::costs::CostRates::default(),
    )
    .map_err(|e| StoreError::Data(e.to_string()))?;
    let detail = match col(&head, "detail") {
        DbValue::Json(s) => match PyValue::from_json(s.as_bytes()) {
            Ok(PyValue::Map(m)) => m,
            _ => PyDict::new(),
        },
        _ => PyDict::new(),
    };
    let c = &figures.costs;
    let costs_total = c.total().map_err(|e| StoreError::Data(e.to_string()))?;
    let costs_json = format!(
        "{{\"costs\": {{\"orders\": {}, \"brokerage\": \"{}\", \"stt\": \"{}\", \
         \"exchange_txn\": \"{}\", \"sebi\": \"{}\", \"ipft\": \"{}\", \"stamp\": \"{}\", \
         \"gst\": \"{}\"}}, \"fills\": {}}}",
        c.orders,
        py::dstr(c.brokerage),
        py::dstr(c.stt),
        py::dstr(c.exchange_txn),
        py::dstr(c.sebi),
        py::dstr(c.ipft),
        py::dstr(c.stamp),
        py::dstr(c.gst),
        fills.len()
    );
    let trade_date = day(col(&head, "trade_date"))?;
    let expiry = if py::truthy(col(&head, "expiry_used")) {
        day(col(&head, "expiry_used"))?
    } else {
        trade_date
    };
    let half = detail.get("half_size").is_some_and(PyValue::truthy);
    let sql = format!(
        "INSERT INTO {} (session_id, user_id, sleeve, trade_date, expiry_used, \
         structure, entry_inr, exit_inr, gross_pnl_inr, costs_inr, net_pnl_inr, risk_budget_inr, \
         r_multiple, closed_reason, minutes_held, mae_r, mfe_r, simulated, sizing_mode, half_size, \
         detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) \
         ON CONFLICT (session_id) DO NOTHING",
        store.t("op_journal")
    );
    let short: String = reason.chars().take(32).collect();
    store
        .conn
        .execute(
            &sql,
            &[
                Param::Int(session_id),
                Param::Int(store.user_id),
                Param::Text(text(col(&head, "sleeve"))),
                Param::Date(trade_date),
                Param::Date(expiry),
                Param::Text(text(col(&head, "structure"))),
                Param::Decimal(figures.entry_inr),
                Param::Decimal(figures.exit_inr),
                Param::Decimal(figures.gross_pnl_inr),
                Param::Decimal(costs_total),
                Param::Decimal(figures.net_pnl_inr),
                Param::Decimal(figures.r_inr),
                Param::Decimal(figures.r_multiple),
                Param::Text(short),
                Param::Int(figures.minutes_held),
                py::pdec(figures.mae_r),
                py::pdec(figures.mfe_r),
                Param::Bool(py::truthy(col(&head, "simulated"))),
                match col(&head, "sizing_mode") {
                    DbValue::Null => Param::Null,
                    v => Param::Text(text(v)),
                },
                Param::Bool(half),
                Param::Text(costs_json),
            ],
        )
        .await?;
    let sql = format!(
        "UPDATE {} SET pnl_inr = ?, pnl_r = ? WHERE id = ?",
        store.t("op_session")
    );
    store
        .conn
        .execute(
            &sql,
            &[
                Param::Decimal(figures.net_pnl_inr),
                Param::Decimal(figures.r_multiple),
                Param::Int(session_id),
            ],
        )
        .await?;
    Ok(Some(Written {
        r_inr: figures.r_inr,
    }))
}

async fn trading_days(
    store: &mut PgOptionsStore<'_>,
    today: NaiveDate,
) -> Result<Vec<NaiveDate>, StoreError> {
    let sql = format!(
        "SELECT date FROM {} WHERE is_trading_day AND date BETWEEN ? AND ?",
        store.t("trading_day")
    );
    let rows = store
        .conn
        .execute(
            &sql,
            &[Param::Date(today), Param::Date(today + Duration::days(7))],
        )
        .await?
        .rows;
    let mut days = Vec::with_capacity(rows.len());
    for r in &rows {
        days.push(day(col(r, "date"))?);
    }
    if !days.is_empty() {
        return Ok(days);
    }
    Ok((0..8)
        .map(|i| today + Duration::days(i))
        .filter(|d| !matches!(d.weekday(), Weekday::Sat | Weekday::Sun))
        .collect())
}

async fn history(
    store: &mut PgOptionsStore<'_>,
    sleeve: Sleeve,
    today: NaiveDate,
    simulated: bool,
) -> Result<Vec<RealisedTrade>, StoreError> {
    let sql = format!(
        "SELECT trade_date, net_pnl_inr, r_multiple FROM {} \
         WHERE user_id = ? AND sleeve = ? AND simulated = ? AND trade_date BETWEEN ? AND ?",
        store.t("op_journal")
    );
    let rows = store
        .conn
        .execute(
            &sql,
            &[
                Param::Int(store.user_id),
                Param::Text(sleeve_str(sleeve).to_owned()),
                Param::Bool(simulated),
                Param::Date(today - Duration::days(40)),
                Param::Date(today),
            ],
        )
        .await?
        .rows;
    let mut out = Vec::with_capacity(rows.len());
    for r in &rows {
        out.push(RealisedTrade {
            sleeve,
            trade_date: day(col(r, "trade_date"))?,
            net_pnl_inr: d(col(r, "net_pnl_inr")),
            r_multiple: d(col(r, "r_multiple")),
        });
    }
    Ok(out)
}

async fn write_pause(
    store: &mut PgOptionsStore<'_>,
    scope: &str,
    pause: &Pause,
) -> Result<bool, StoreError> {
    let book = scope == "BOOK";
    let table = store.t(if book {
        "op_book_config"
    } else {
        "op_sleeve_config"
    });
    let key_sql = if book {
        "user_id = ?"
    } else {
        "user_id = ? AND sleeve = ?"
    };
    let mut key = vec![Param::Int(store.user_id)];
    if !book {
        key.push(Param::Text(scope.to_owned()));
    }
    let sql = format!("SELECT paused_until FROM {table} WHERE {key_sql}");
    let row = store
        .conn
        .execute(&sql, &key)
        .await?
        .rows
        .into_iter()
        .next();
    let old = match &row {
        Some(r) if !col(r, "paused_until").is_null() => Some(day(col(r, "paused_until"))?),
        _ => None,
    };
    let until = old.map_or(pause.paused_until, |o| o.max(pause.paused_until));
    let reason: String = pause
        .reasons
        .iter()
        .map(|r| r.as_str())
        .collect::<Vec<_>>()
        .join(",")
        .chars()
        .take(32)
        .collect();
    if row.is_none() {
        let (cols, marks) = if book {
            ("user_id, paused_until, paused_reason", "?, ?, ?")
        } else {
            ("user_id, sleeve, paused_until, paused_reason", "?, ?, ?, ?")
        };
        let mut params = key.clone();
        params.push(Param::Date(until));
        params.push(Param::Text(reason.clone()));
        let sql = format!("INSERT INTO {table} ({cols}) VALUES ({marks})");
        store.conn.execute(&sql, &params).await?;
    } else if old == Some(until) {
        return Ok(false);
    } else {
        let mut params = vec![Param::Date(until), Param::Text(reason.clone())];
        params.extend(key.iter().cloned());
        let sql = format!(
            "UPDATE {table} SET paused_until = ?, paused_reason = ?, updated_by = 'ledger' \
             WHERE {key_sql}"
        );
        store.conn.execute(&sql, &params).await?;
    }
    let sql = format!(
        "INSERT INTO {} (user_id, scope, key, old_value, new_value, \
         changed_by, note) VALUES (?, ?, 'paused_until', ?, ?, 'ledger', ?)",
        store.t("op_config_audit")
    );
    store
        .conn
        .execute(
            &sql,
            &[
                Param::Int(store.user_id),
                Param::Text(scope.to_owned()),
                old.map_or(Param::Null, |o| {
                    Param::Text(o.format("%Y-%m-%d").to_string())
                }),
                Param::Text(until.format("%Y-%m-%d").to_string()),
                Param::Text(reason),
            ],
        )
        .await?;
    Ok(true)
}

async fn open_marked_inr(
    store: &mut PgOptionsStore<'_>,
    sleeve: Option<Sleeve>,
    simulated: bool,
) -> Result<Decimal, StoreError> {
    let sql = format!(
        "SELECT pos.entry_points, pos.last_mark_points, pos.lots, p.lot_size, p.structure, s.sleeve \
         FROM {} pos JOIN {} s ON s.id = pos.session_id \
         JOIN {} p ON p.plan_id = s.plan_id \
         WHERE pos.user_id = ? AND pos.closed_at IS NULL AND pos.simulated = ?",
        store.t("op_position"),
        store.t("op_session"),
        store.t("op_plan")
    );
    let rows = store
        .conn
        .execute(&sql, &[Param::Int(store.user_id), Param::Bool(simulated)])
        .await?
        .rows;
    let mut total = Decimal::ZERO;
    for r in &rows {
        if let Some(s) = sleeve
            && text(col(r, "sleeve")) != sleeve_str(s)
        {
            continue;
        }
        if col(r, "last_mark_points").is_null() {
            continue;
        }
        let st = text(col(r, "structure"));
        let structure = Structure::parse(&st).ok_or_else(|| {
            StoreError::Data(format!("ValueError: '{st}' is not a valid Structure"))
        })?;
        let pnl = pnl_points(
            structure,
            d(col(r, "entry_points")),
            d(col(r, "last_mark_points")),
        );
        total += pnl * Decimal::from(int(col(r, "lots"))) * Decimal::from(int(col(r, "lot_size")));
    }
    Ok(total)
}

async fn close_open(
    store: &mut PgOptionsStore<'_>,
    sleeve: Option<Sleeve>,
    code: &str,
    now: DateTime<FixedOffset>,
) -> Result<Vec<i64>, StoreError> {
    let positions = store.open_positions(now.date_naive()).await?;
    let mut raised = Vec::new();
    for tracked in positions {
        if let Some(s) = sleeve
            && tracked.sleeve != s
        {
            continue;
        }
        if tracked.exit_plan_id.is_none() {
            store
                .raise_exit(tracked.session_id, &ExitVerdict::rule(code, "LEDGER"), now)
                .await?;
            raised.push(tracked.session_id);
        }
    }
    Ok(raised)
}

/// `apply_ledger(store, sleeve=, today=, now=, r_today_inr=, mode=)`: §9.1 then §9.3; every
/// pause reason applied.
///
/// # Errors
/// The database.
pub async fn apply_ledger(
    store: &mut PgOptionsStore<'_>,
    sleeve: Sleeve,
    today: NaiveDate,
    now: DateTime<FixedOffset>,
    r_today_inr: Decimal,
    mode: Mode,
) -> Result<Vec<String>, StoreError> {
    let cfg = RiskConfig::default();
    let simulated = mode == Mode::Paper;
    let mut applied = Vec::new();
    let hist = history(store, sleeve, today, simulated).await?;
    let marked = open_marked_inr(store, Some(sleeve), simulated).await?;
    let days = trading_days(store, today).await?;
    let r_today = if r_today_inr > Decimal::ZERO {
        r_today_inr
    } else {
        Decimal::ONE
    };
    let pause = evaluate_sleeve(sleeve, today, &hist, marked, r_today, &days, &cfg)
        .map_err(|m| StoreError::Data(format!("ValueError: {m}")))?;
    if let Some(p) = pause {
        write_pause(store, group_str(group_of(sleeve)), &p).await?;
        applied.extend(p.reasons.iter().map(|r| r.as_str().to_owned()));
        if p.close_now
            && let Some(first) = p.reasons.first()
        {
            close_open(store, Some(sleeve), first.as_str(), now).await?;
        }
    }
    let sql = format!(
        "SELECT sleeve_capital_inr FROM {} WHERE user_id = ?",
        store.t("op_sleeve_config")
    );
    let capitals: Vec<Decimal> = store
        .conn
        .execute(&sql, &[Param::Int(store.user_id)])
        .await?
        .rows
        .iter()
        .map(|r| d(col(r, "sleeve_capital_inr")))
        .collect();
    let sql = format!(
        "SELECT daily_loss_limit_inr, monthly_pause_inr FROM {} WHERE user_id = ?",
        store.t("op_book_config")
    );
    let book = store
        .conn
        .execute(&sql, &[Param::Int(store.user_id)])
        .await?
        .rows
        .into_iter()
        .next();
    let limits = book_limits(
        &capitals,
        book.as_ref()
            .map_or(Decimal::ZERO, |b| d(col(b, "daily_loss_limit_inr"))),
        book.as_ref()
            .map_or(Decimal::ZERO, |b| d(col(b, "monthly_pause_inr"))),
        &cfg,
        &BookCeilings::default(),
    );
    let Some(limits) = limits else {
        return Ok(applied);
    };
    let sql = format!(
        "SELECT COALESCE(SUM(CASE WHEN trade_date = ? THEN net_pnl_inr END), 0) AS today, \
         COALESCE(SUM(net_pnl_inr), 0) AS month FROM {} \
         WHERE user_id = ? AND simulated = ? AND trade_date BETWEEN ? AND ?",
        store.t("op_journal")
    );
    let first = today.with_day(1).unwrap_or(today);
    let sums = store
        .conn
        .execute(
            &sql,
            &[
                Param::Date(today),
                Param::Int(store.user_id),
                Param::Bool(simulated),
                Param::Date(first),
                Param::Date(today),
            ],
        )
        .await?
        .rows
        .into_iter()
        .next()
        .unwrap_or_default();
    let open_all = open_marked_inr(store, None, simulated).await?;
    let book_pause = evaluate_book(
        today,
        d(col(&sums, "today")) + open_all,
        d(col(&sums, "month")),
        Some(&limits),
    );
    if let Some(p) = book_pause {
        write_pause(store, "BOOK", &p).await?;
        applied.extend(p.reasons.iter().map(|r| r.as_str().to_owned()));
        if p.close_now
            && let Some(first) = p.reasons.first()
        {
            close_open(store, None, first.as_str(), now).await?;
        }
    }
    Ok(applied)
}

/// `after_close(store, session_id, sleeve=, reason=, now=, mode=)`.
///
/// # Errors
/// The database.
pub async fn after_close(
    store: &mut PgOptionsStore<'_>,
    session_id: i64,
    sleeve: Sleeve,
    reason: &str,
    now: DateTime<FixedOffset>,
    mode: Mode,
) -> Result<Vec<String>, StoreError> {
    let figures = write_journal(store, session_id, reason, now).await?;
    let r_today = figures.map_or(Decimal::ZERO, |f| f.r_inr);
    let today = now.with_timezone(&py::ist()).date_naive();
    apply_ledger(store, sleeve, today, now, r_today, mode).await
}

/// `morning_ledger(store, now=, mode_of=)`: every sleeve's rules before the open.
///
/// # Errors
/// The database.
pub async fn morning_ledger(
    store: &mut PgOptionsStore<'_>,
    now: DateTime<FixedOffset>,
    mode_of: &(dyn Fn(Sleeve) -> Mode + Sync),
) -> Result<Vec<(Sleeve, Vec<String>)>, StoreError> {
    let today = now.with_timezone(&py::ist()).date_naive();
    let mut out = Vec::new();
    for sleeve in SLEEVES {
        let applied =
            apply_ledger(store, sleeve, today, now, Decimal::ONE, mode_of(sleeve)).await?;
        out.push((sleeve, applied));
    }
    Ok(out)
}

/// Unused import guard (the `Side` enum is part of this module's API surface via `side_of`).
#[allow(dead_code)]
const fn _side(_: Side) {}
