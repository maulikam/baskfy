//! Ports `options_execute.PgOptionsStore` and `options_monitor.PgPositionStore`: the options
//! book over the screener's `op_` tables (schema `public`) through the desk's Postgres adapter.
//! Every statement names its schema and its user, and is Python's text verbatim.

use chrono::{DateTime, Duration, FixedOffset, NaiveDate, NaiveTime};
use rust_decimal::Decimal;

use super::core::{
    LegRole, Side, Sleeve, Structure, role_of, side_of, side_str, sleeve_of, sleeve_str,
};
use super::py::{self, col, int, text};
use crate::storage::{Conn, Param, Row, StorageError};
use crate::wire::{PyDict, PyValue};

/// `SCHEMA`.
pub const SCHEMA: &str = "public";

/// `options_monitor.SESSION_CLOSE`.
#[must_use]
pub fn session_close() -> NaiveTime {
    NaiveTime::from_hms_opt(15, 30, 0).unwrap_or(NaiveTime::MIN)
}

/// What a store call can fail with.
#[derive(Debug, thiserror::Error)]
pub enum StoreError {
    /// The database.
    #[error("{0}")]
    Db(#[from] StorageError),
    /// A row Python would have raised on (`ValueError`, `KeyError`, `TypeError`).
    #[error("{0}")]
    Data(String),
}

impl StoreError {
    /// `f"{type(exc).__name__}: {exc}"` as the pages print it.
    #[must_use]
    pub fn py_text(&self) -> String {
        match self {
            Self::Db(e) => format!("{}: {e}", db_error_type(e)),
            Self::Data(m) => m.clone(),
        }
    }
}

/// The Python exception class a driver error surfaces as (psycopg's names).
#[must_use]
pub fn db_error_type(e: &StorageError) -> &'static str {
    match e {
        StorageError::Driver(sqlx::Error::Database(d)) => match d.code().as_deref() {
            Some("42P01") => "UndefinedTable",
            Some("42703") => "UndefinedColumn",
            Some("23505") => "UniqueViolation",
            Some("22P02") => "InvalidTextRepresentation",
            _ => "DatabaseError",
        },
        StorageError::Driver(_) => "OperationalError",
        _ => "ProgrammingError",
    }
}

/// `_t(schema, table)`.
#[must_use]
pub fn t(schema: &str, table: &str) -> String {
    if schema.is_empty() {
        table.to_owned()
    } else {
        format!("{schema}.{table}")
    }
}

/// `LegRow`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LegRow {
    /// `op_leg.id`.
    pub id: i64,
    /// Send order.
    pub seq: i64,
    /// The role.
    pub role: LegRole,
    /// The contract.
    pub tradingsymbol: String,
    /// Kite's token.
    pub instrument_token: i64,
    /// The entry side.
    pub side: Side,
    /// Units.
    pub quantity: i64,
}

/// `PlanRow`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PlanRow {
    /// `op_plan.id`.
    pub pk: i64,
    /// `op_plan.plan_id`.
    pub plan_id: String,
    /// `op_session.id`.
    pub session_id: i64,
    /// The sleeve.
    pub sleeve: Sleeve,
    /// The structure.
    pub structure: Structure,
    /// `ENTRY` / `EXIT`.
    pub kind: String,
    /// The plan's status.
    pub status: String,
    /// The window's end.
    pub expires_at: DateTime<FixedOffset>,
    /// Lots.
    pub lots: i64,
    /// Units per lot.
    pub lot_size: i64,
    /// The session's day.
    pub trade_date: NaiveDate,
    /// The session's state.
    pub session_state: String,
    /// Legs in send order.
    pub legs: Vec<LegRow>,
}

fn data(msg: impl Into<String>) -> StoreError {
    StoreError::Data(msg.into())
}

fn sleeve_col(v: &crate::storage::DbValue) -> Result<Sleeve, StoreError> {
    let s = text(v);
    sleeve_of(&s).ok_or_else(|| data(format!("ValueError: '{s}' is not a valid Sleeve")))
}

/// `PgOptionsStore`.
pub struct PgOptionsStore<'c> {
    /// The connection (`store.conn`).
    pub conn: &'c mut Conn,
    /// `user_id`.
    pub user_id: i64,
    /// `schema`.
    pub schema: String,
}

impl<'c> PgOptionsStore<'c> {
    /// `PgOptionsStore(conn, user_id=, schema=SCHEMA)`.
    pub fn new(conn: &'c mut Conn, user_id: i64) -> Self {
        Self {
            conn,
            user_id,
            schema: SCHEMA.to_owned(),
        }
    }

    /// `store.t(table)`.
    #[must_use]
    pub fn t(&self, table: &str) -> String {
        t(&self.schema, table)
    }

    async fn rows(&mut self, sql: &str, params: &[Param]) -> Result<Vec<Row>, StoreError> {
        Ok(self.conn.execute(sql, params).await?.rows)
    }

    async fn one(&mut self, sql: &str, params: &[Param]) -> Result<Option<Row>, StoreError> {
        Ok(self
            .conn
            .execute(sql, params)
            .await?
            .rows
            .into_iter()
            .next())
    }

    /// `store.plan(plan_id)`.
    ///
    /// # Errors
    /// The database, or a row Python would refuse.
    pub async fn plan(&mut self, plan_id: &str) -> Result<Option<PlanRow>, StoreError> {
        let sql = format!(
            "SELECT p.id, p.plan_id, p.session_id, p.sleeve, p.structure, p.kind, p.status, \
             p.expires_at, p.lots, p.lot_size, s.trade_date, s.state AS session_state \
             FROM {} p JOIN {} s ON s.id = p.session_id \
             WHERE p.user_id = ? AND p.plan_id = ?",
            self.t("op_plan"),
            self.t("op_session")
        );
        let Some(row) = self
            .one(
                &sql,
                &[Param::Int(self.user_id), Param::Text(plan_id.to_owned())],
            )
            .await?
        else {
            return Ok(None);
        };
        let legs_sql = format!(
            "SELECT id, seq, role, tradingsymbol, instrument_token, side, quantity \
             FROM {} WHERE plan_id = ? ORDER BY seq",
            self.t("op_leg")
        );
        let legs = self
            .rows(&legs_sql, &[Param::Int(int(col(&row, "id")))])
            .await?;
        let mut out_legs = Vec::with_capacity(legs.len());
        for lg in &legs {
            let role_text = text(col(lg, "role"));
            let side_text = text(col(lg, "side"));
            out_legs.push(LegRow {
                id: int(col(lg, "id")),
                seq: int(col(lg, "seq")),
                role: role_of(&role_text).ok_or_else(|| {
                    data(format!("ValueError: '{role_text}' is not a valid LegRole"))
                })?,
                tradingsymbol: text(col(lg, "tradingsymbol")),
                instrument_token: int(col(lg, "instrument_token")),
                side: side_of(&side_text).ok_or_else(|| {
                    data(format!("ValueError: '{side_text}' is not a valid Side"))
                })?,
                quantity: int(col(lg, "quantity")),
            });
        }
        let structure_text = text(col(&row, "structure"));
        Ok(Some(PlanRow {
            pk: int(col(&row, "id")),
            plan_id: text(col(&row, "plan_id")),
            session_id: int(col(&row, "session_id")),
            sleeve: sleeve_col(col(&row, "sleeve"))?,
            structure: super::core::structure_of(&structure_text).ok_or_else(|| {
                data(format!(
                    "ValueError: '{structure_text}' is not a valid Structure"
                ))
            })?,
            kind: text(col(&row, "kind")),
            status: text(col(&row, "status")),
            expires_at: py::aware(col(&row, "expires_at"))
                .ok_or_else(|| data("TypeError: expires_at is not a datetime"))?,
            lots: int(col(&row, "lots")),
            lot_size: int(col(&row, "lot_size")),
            trade_date: py::opt_day(col(&row, "trade_date"))
                .ok_or_else(|| data("TypeError: trade_date is not a date"))?,
            session_state: text(col(&row, "session_state")),
            legs: out_legs,
        }))
    }

    /// `store.slot_holder(day)`.
    ///
    /// # Errors
    /// The database.
    pub async fn slot_holder(&mut self, day: NaiveDate) -> Result<Option<Sleeve>, StoreError> {
        let sql = format!(
            "SELECT slot_holder FROM {} WHERE user_id = ? AND trade_date = ? \
             AND slot_holder IS NOT NULL LIMIT 1",
            self.t("op_session")
        );
        match self
            .one(&sql, &[Param::Int(self.user_id), Param::Date(day)])
            .await?
        {
            None => Ok(None),
            Some(r) => sleeve_col(col(&r, "slot_holder")).map(Some),
        }
    }

    /// `store.confirm(plan, at, slot)`.
    ///
    /// # Errors
    /// The database.
    pub async fn confirm(
        &mut self,
        plan: &PlanRow,
        at: DateTime<FixedOffset>,
        slot: Option<Sleeve>,
    ) -> Result<(), StoreError> {
        let sql = format!(
            "UPDATE {} SET status = 'CONFIRMED', confirmed_at = ? \
             WHERE id = ? AND status = 'ISSUED'",
            self.t("op_plan")
        );
        self.conn
            .execute(&sql, &[Param::Aware(at), Param::Int(plan.pk)])
            .await?;
        let holder = match slot {
            Some(s) if s == plan.sleeve => Param::Text(sleeve_str(s).to_owned()),
            _ => Param::Null,
        };
        let sql = format!(
            "UPDATE {} SET state = 'CONFIRMED', slot_holder = ? \
             WHERE id = ? AND state = 'PLANNED'",
            self.t("op_session")
        );
        self.conn
            .execute(&sql, &[holder, Param::Int(plan.session_id)])
            .await?;
        Ok(())
    }

    /// `store.close_orders(leg_id, side)`.
    ///
    /// # Errors
    /// The database.
    pub async fn close_orders(&mut self, leg_id: i64) -> Result<i64, StoreError> {
        let sql = format!(
            "SELECT count(*) AS n FROM {} o JOIN {} l \
             ON l.id = o.leg_id WHERE o.leg_id = ? AND o.side <> l.side",
            self.t("op_order"),
            self.t("op_leg")
        );
        Ok(self
            .one(&sql, &[Param::Int(leg_id)])
            .await?
            .map_or(0, |r| int(col(&r, "n"))))
    }

    /// `store.write_order(...)`: one `op_order` row; its id.
    ///
    /// # Errors
    /// The database.
    #[allow(clippy::too_many_arguments)]
    pub async fn write_order(
        &mut self,
        leg: &LegRow,
        client_id: &str,
        side: Side,
        quantity: i64,
        limit: Decimal,
        result: &PyDict,
        simulated: bool,
    ) -> Result<i64, StoreError> {
        let sql = format!(
            "INSERT INTO {} (user_id, leg_id, client_id, side, quantity, \
             limit_price, broker_order_id, gateway_status, gateway_result, simulated) \
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            self.t("op_order")
        );
        let order_id = match result.get("order_id") {
            None | Some(PyValue::None) => Param::Null,
            Some(PyValue::Str(s)) => Param::Text(s.clone()),
            Some(PyValue::Int(i)) => Param::Int(*i),
            Some(other) => Param::Text(other.py_str()),
        };
        let status = result
            .get("status")
            .map_or_else(|| "None".to_owned(), PyValue::py_str);
        let dumped = crate::wire::py_json_dumps(&PyValue::Map(result.clone()))
            .unwrap_or_else(|_| "{}".to_owned());
        let row = self
            .one(
                &sql,
                &[
                    Param::Int(self.user_id),
                    Param::Int(leg.id),
                    Param::Text(client_id.to_owned()),
                    Param::Text(side_str(side).to_owned()),
                    Param::Int(quantity),
                    Param::Decimal(limit),
                    order_id,
                    Param::Text(status),
                    Param::Text(dumped),
                    Param::Bool(simulated),
                ],
            )
            .await?
            .ok_or_else(|| data("TypeError: 'NoneType' object is not subscriptable"))?;
        Ok(int(col(&row, "id")))
    }

    /// `store.write_fill(...)`.
    ///
    /// # Errors
    /// The database.
    #[allow(clippy::too_many_arguments)]
    pub async fn write_fill(
        &mut self,
        leg: &LegRow,
        order_id: i64,
        at: DateTime<FixedOffset>,
        filled: i64,
        avg_price: Option<Decimal>,
        requested: i64,
        levels_consumed: i64,
        limit: Decimal,
    ) -> Result<(), StoreError> {
        let sql = format!(
            "INSERT INTO {} (user_id, leg_id, order_id, filled_at, quantity, \
             price, simulated, sim_method, detail) VALUES (?, ?, ?, ?, ?, ?, true, ?, ?)",
            self.t("op_fill")
        );
        let detail = format!(
            "{{\"limit\": \"{}\", \"requested\": {requested}, \"levels_consumed\": {levels_consumed}}}",
            py::dstr(limit)
        );
        self.conn
            .execute(
                &sql,
                &[
                    Param::Int(self.user_id),
                    Param::Int(leg.id),
                    Param::Int(order_id),
                    Param::Aware(at),
                    Param::Int(filled),
                    Param::Decimal(py::q2(avg_price.unwrap_or(Decimal::ZERO))),
                    Param::Text("DEPTH_LADDER".to_owned()),
                    Param::Text(detail),
                ],
            )
            .await?;
        Ok(())
    }

    /// `store.settle_legs(plan, result)`.
    ///
    /// # Errors
    /// The database.
    pub async fn settle_legs(
        &mut self,
        plan: &PlanRow,
        result: &super::core::ExecResult,
    ) -> Result<(), StoreError> {
        let sql = format!(
            "UPDATE {} SET filled_qty = ?, avg_price = ?, status = ? WHERE id = ?",
            self.t("op_leg")
        );
        for leg in &plan.legs {
            let filled: i64 = result
                .fills
                .iter()
                .find(|(r, _)| *r == leg.role)
                .map_or(0, |(_, f)| f.iter().map(|x| x.quantity).sum());
            let price = result.avg_price(leg.role);
            let status = if filled >= leg.quantity {
                "FILLED"
            } else if filled != 0 {
                "PARTIAL"
            } else {
                "CANCELLED"
            };
            self.conn
                .execute(
                    &sql,
                    &[
                        Param::Int(filled.min(leg.quantity)),
                        py::pdec(price.map(py::q2)),
                        Param::Text(status.to_owned()),
                        Param::Int(leg.id),
                    ],
                )
                .await?;
        }
        Ok(())
    }

    /// `store.open_position(plan, result, entry_points, at, hard_exit_at)`.
    ///
    /// # Errors
    /// The database.
    pub async fn open_position(
        &mut self,
        plan: &PlanRow,
        entry_points: Decimal,
        at: DateTime<FixedOffset>,
        hard_exit_at: DateTime<FixedOffset>,
    ) -> Result<(), StoreError> {
        let quantity = plan.lots * plan.lot_size;
        let ids: Vec<i64> = plan.legs.iter().map(|l| l.id).collect();
        let sql = format!(
            "INSERT INTO {} (session_id, user_id, leg_ids, entry_points, \
             entry_inr, lots, opened_at, hard_exit_at, simulated) \
             VALUES (?, ?, {}, ?, ?, ?, ?, ?, true) ON CONFLICT (session_id) DO NOTHING",
            self.t("op_position"),
            py::int_array_sql(&ids)
        );
        self.conn
            .execute(
                &sql,
                &[
                    Param::Int(plan.session_id),
                    Param::Int(self.user_id),
                    Param::Decimal(entry_points),
                    Param::Decimal(py::q2(entry_points * Decimal::from(quantity))),
                    Param::Int(plan.lots),
                    Param::Aware(at),
                    Param::Aware(hard_exit_at),
                ],
            )
            .await?;
        let sql = format!(
            "UPDATE {} SET state = 'OPEN' WHERE id = ? AND state = 'CONFIRMED'",
            self.t("op_session")
        );
        self.conn
            .execute(&sql, &[Param::Int(plan.session_id)])
            .await?;
        Ok(())
    }

    /// `store.abandon(plan, at)`.
    ///
    /// # Errors
    /// The database.
    pub async fn abandon(&mut self, plan: &PlanRow) -> Result<(), StoreError> {
        let sql = format!(
            "UPDATE {} SET state = 'CLOSED', closed_reason = 'ABANDONED_ENTRY', \
             slot_holder = NULL WHERE id = ? AND state = 'CONFIRMED'",
            self.t("op_session")
        );
        self.conn
            .execute(&sql, &[Param::Int(plan.session_id)])
            .await?;
        Ok(())
    }

    /// `store.net_open(session_id)`: open quantity per role (`GROUP BY l.role` order).
    ///
    /// # Errors
    /// The database.
    pub async fn net_open(&mut self, session_id: i64) -> Result<super::core::Position, StoreError> {
        let sql = format!(
            "SELECT l.role, COALESCE(SUM(CASE WHEN o.side = l.side THEN f.quantity \
             ELSE -f.quantity END), 0) AS open_qty \
             FROM {} s JOIN {} p ON p.plan_id = s.plan_id \
             JOIN {} l ON l.plan_id = p.id \
             LEFT JOIN {} f ON f.leg_id = l.id \
             LEFT JOIN {} o ON o.id = f.order_id \
             WHERE s.id = ? AND s.user_id = ? GROUP BY l.role",
            self.t("op_session"),
            self.t("op_plan"),
            self.t("op_leg"),
            self.t("op_fill"),
            self.t("op_order")
        );
        let rows = self
            .rows(&sql, &[Param::Int(session_id), Param::Int(self.user_id)])
            .await?;
        let mut out = Vec::new();
        for r in &rows {
            let role_text = text(col(r, "role"));
            let role = role_of(&role_text)
                .ok_or_else(|| data(format!("ValueError: '{role_text}' is not a valid LegRole")))?;
            let qty = int(col(r, "open_qty"));
            if let Some(slot) = out
                .iter_mut()
                .find(|(k, _): &&mut (LegRole, i64)| *k == role)
            {
                slot.1 = qty;
            } else {
                out.push((role, qty));
            }
        }
        Ok(out)
    }

    /// `store.close_position(session_id, exit_plan_id, at, reason)`.
    ///
    /// # Errors
    /// The database.
    pub async fn close_position(
        &mut self,
        session_id: i64,
        exit_plan_id: Option<&str>,
        at: DateTime<FixedOffset>,
        reason: &str,
    ) -> Result<(), StoreError> {
        let sql = format!(
            "UPDATE {} SET closed_at = ? WHERE session_id = ? AND closed_at IS NULL",
            self.t("op_position")
        );
        self.conn
            .execute(&sql, &[Param::Aware(at), Param::Int(session_id)])
            .await?;
        let sql = format!(
            "UPDATE {} SET state = 'CLOSED', closed_reason = ? WHERE id = ? AND state = 'OPEN'",
            self.t("op_session")
        );
        let short: String = reason.chars().take(32).collect();
        self.conn
            .execute(&sql, &[Param::Text(short), Param::Int(session_id)])
            .await?;
        if let Some(exit) = exit_plan_id {
            let sql = format!(
                "UPDATE {} SET status = 'CONFIRMED', confirmed_at = ? \
                 WHERE plan_id = ? AND user_id = ?",
                self.t("op_plan")
            );
            self.conn
                .execute(
                    &sql,
                    &[
                        Param::Aware(at),
                        Param::Text(exit.to_owned()),
                        Param::Int(self.user_id),
                    ],
                )
                .await?;
        }
        Ok(())
    }

    /// `entry_plan_for(store, session_id)`.
    ///
    /// # Errors
    /// The database.
    pub async fn entry_plan_for(&mut self, session_id: i64) -> Result<Option<PlanRow>, StoreError> {
        let sql = format!(
            "SELECT plan_id FROM {} WHERE id = ? AND user_id = ?",
            self.t("op_session")
        );
        let row = self
            .one(&sql, &[Param::Int(session_id), Param::Int(self.user_id)])
            .await?;
        match row {
            Some(r) if !col(&r, "plan_id").is_null() => {
                let id = text(col(&r, "plan_id"));
                self.plan(&id).await
            }
            _ => Ok(None),
        }
    }

    // --- PgPositionStore ------------------------------------------------------------------------

    /// `PgPositionStore.open_positions(day)`.
    ///
    /// # Errors
    /// The database, or a row Python would refuse.
    pub async fn open_positions(&mut self, day: NaiveDate) -> Result<Vec<Tracked>, StoreError> {
        let s = self.schema.clone();
        let sql = format!(
            "SELECT p.session_id, p.entry_points, p.opened_at, p.exit_plan_id, p.leg_ids, \
             s.sleeve, pl.structure, pl.risk_budget_inr, pl.width_points, pl.detail \
             FROM {} p \
             JOIN {} s ON s.id = p.session_id \
             JOIN {} pl ON pl.plan_id = s.plan_id \
             WHERE p.user_id = ? AND s.trade_date = ? AND p.closed_at IS NULL \
             ORDER BY p.session_id",
            t(&s, "op_position"),
            t(&s, "op_session"),
            t(&s, "op_plan")
        );
        let heads = self
            .rows(&sql, &[Param::Int(self.user_id), Param::Date(day)])
            .await?;
        let mut out = Vec::with_capacity(heads.len());
        for head in &heads {
            let ids = py::int_array(col(head, "leg_ids"));
            let legs_sql = format!(
                "SELECT id, role, strike, quantity, filled_qty, avg_price, instrument_token \
                 FROM {} WHERE id = ANY({}) ORDER BY seq",
                t(&s, "op_leg"),
                py::int_array_sql(&ids)
            );
            let legs = self.rows(&legs_sql, &[]).await?;
            out.push(position_from_rows(head, &legs)?);
        }
        Ok(out)
    }

    /// `PgPositionStore.raise_exit(tracked, verdict, at)`: one `kind='EXIT'` plan
    /// (`<entry>-X`, deterministic) and `op_position.exit_plan_id` where still null.
    ///
    /// # Errors
    /// The database.
    pub async fn raise_exit(
        &mut self,
        session_id: i64,
        verdict: &ExitVerdict,
        at: DateTime<FixedOffset>,
    ) -> Result<Option<String>, StoreError> {
        let s = self.schema.clone();
        let sql = format!(
            "SELECT pl.plan_id, pl.sleeve, pl.structure, pl.sizing_mode, pl.lots, pl.lot_size \
             FROM {} se JOIN {} pl ON pl.plan_id = se.plan_id WHERE se.id = ?",
            t(&s, "op_session"),
            t(&s, "op_plan")
        );
        let Some(entry) = self.one(&sql, &[Param::Int(session_id)]).await? else {
            return Ok(None);
        };
        let exit_id = format!("{}-X", text(col(&entry, "plan_id")));
        let issued = at;
        // `issued.date()` is the aware value's own calendar date.
        let close = py::ist_at(issued.date_naive(), session_close()).unwrap_or(issued);
        let plus = issued + Duration::minutes(1);
        let expires = if plus > close { plus } else { close };
        let mut detail = PyDict::new();
        detail.insert("code", PyValue::Str(verdict.code.clone()));
        detail.insert("reason", PyValue::Str(verdict.reason.clone()));
        detail.insert("value_points", py::vdec(verdict.value_points));
        detail.insert(
            "marked_loss_inr",
            py::vdec(verdict.marked_loss_inr.map(py::q2)),
        );
        detail.insert("stale", PyValue::Bool(verdict.stale));
        detail.insert("entry_plan_id", py::db_to_py(col(&entry, "plan_id")));
        let dumped =
            crate::wire::py_json_dumps(&PyValue::Map(detail)).unwrap_or_else(|_| "{}".to_owned());
        let sql = format!(
            "INSERT INTO {} (user_id, plan_id, session_id, sleeve, structure, \
             kind, sizing_mode, issued_at, expires_at, lots, lot_size, status, detail) \
             VALUES (?, ?, ?, ?, ?, 'EXIT', ?, ?, ?, ?, ?, 'ISSUED', ?) \
             ON CONFLICT (plan_id) DO NOTHING",
            t(&s, "op_plan")
        );
        let as_param = |v: &crate::storage::DbValue| match v {
            crate::storage::DbValue::Null => Param::Null,
            crate::storage::DbValue::Int(i) => Param::Int(*i),
            other => Param::Text(text(other)),
        };
        self.conn
            .execute(
                &sql,
                &[
                    Param::Int(self.user_id),
                    Param::Text(exit_id.clone()),
                    Param::Int(session_id),
                    as_param(col(&entry, "sleeve")),
                    as_param(col(&entry, "structure")),
                    as_param(col(&entry, "sizing_mode")),
                    Param::Aware(issued),
                    Param::Aware(expires),
                    as_param(col(&entry, "lots")),
                    as_param(col(&entry, "lot_size")),
                    Param::Text(dumped),
                ],
            )
            .await?;
        let sql = format!(
            "UPDATE {} SET exit_plan_id = ? WHERE session_id = ? AND exit_plan_id IS NULL",
            t(&s, "op_position")
        );
        self.conn
            .execute(
                &sql,
                &[Param::Text(exit_id.clone()), Param::Int(session_id)],
            )
            .await?;
        Ok(Some(exit_id))
    }

    /// `PgPositionStore.record_mark(tracked, value, at)`.
    ///
    /// # Errors
    /// The database.
    pub async fn record_mark(
        &mut self,
        tracked: &Tracked,
        value: Decimal,
        at: DateTime<FixedOffset>,
    ) -> Result<(), StoreError> {
        self.record_mark_of(
            tracked.session_id,
            tracked.structure,
            tracked.entry_points,
            value,
            at,
        )
        .await
    }

    /// `record_mark` from the parts it reads (the monitor holds `exits.OpenPosition`s).
    ///
    /// # Errors
    /// The database.
    pub async fn record_mark_of(
        &mut self,
        session_id: i64,
        structure: Structure,
        entry_points: Decimal,
        value: Decimal,
        at: DateTime<FixedOffset>,
    ) -> Result<(), StoreError> {
        let pnl = py::q2(super::core::pnl_points(structure, entry_points, value));
        let sql = format!(
            "UPDATE {} SET last_mark_points = ?, last_mark_at = ?, \
             peak_value = GREATEST(COALESCE(peak_value, ?), ?), \
             trough_value = LEAST(COALESCE(trough_value, ?), ?) WHERE session_id = ?",
            t(&self.schema, "op_position")
        );
        self.conn
            .execute(
                &sql,
                &[
                    Param::Decimal(py::q2(value)),
                    Param::Aware(at),
                    Param::Decimal(pnl),
                    Param::Decimal(pnl),
                    Param::Decimal(pnl),
                    Param::Decimal(pnl),
                    Param::Int(session_id),
                ],
            )
            .await?;
        Ok(())
    }

    /// `PgPositionStore.index_minutes(day, until)`: NIFTY 50's stored minutes from 09:15 to
    /// `until` (exclusive), as naive IST bars.
    ///
    /// # Errors
    /// The database, or a row Python would refuse.
    pub async fn index_minutes(
        &mut self,
        day: NaiveDate,
        until: DateTime<FixedOffset>,
    ) -> Result<Vec<baskfy_core::options::bars::Bar>, StoreError> {
        let s = self.schema.clone();
        let start = py::ist_at(
            day,
            NaiveTime::from_hms_opt(9, 15, 0).unwrap_or(NaiveTime::MIN),
        )
        .ok_or_else(|| data("ValueError: 09:15 IST"))?;
        let sql = format!(
            "SELECT m.ts, m.open, m.high, m.low, m.close \
             FROM {} m JOIN {} i \
             ON i.id = m.instrument_id \
             WHERE i.kite_token = ? AND m.ts >= ? AND m.ts < ? ORDER BY m.ts",
            t(&s, "op_index_minute"),
            t(&s, "instrument")
        );
        let rows = self
            .rows(
                &sql,
                &[
                    Param::Int(super::monitor::NIFTY_50_TOKEN),
                    Param::Aware(start),
                    Param::Aware(until),
                ],
            )
            .await?;
        let mut out = Vec::with_capacity(rows.len());
        for r in &rows {
            let ts =
                py::aware(col(r, "ts")).ok_or_else(|| data("TypeError: ts is not a datetime"))?;
            let num = |name: &str| {
                py::dec(col(r, name)).ok_or_else(|| {
                    data(format!("decimal.InvalidOperation: {name} is not a number"))
                })
            };
            out.push(baskfy_core::options::bars::Bar {
                ts,
                open: num("open")?,
                high: num("high")?,
                low: num("low")?,
                close: num("close")?,
            });
        }
        Ok(out)
    }
}

/// `exits.ExitVerdict` as the desk writes it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExitVerdict {
    /// `code` (`MANUAL`, `HARD_EXIT`, a pause reason, …).
    pub code: String,
    /// `reason` (`RULE`, `LEDGER`, `FEED_LOST`, …).
    pub reason: String,
    /// The mark that fired it.
    pub value_points: Option<Decimal>,
    /// The marked loss.
    pub marked_loss_inr: Option<Decimal>,
    /// Whether the mark was stale.
    pub stale: bool,
}

impl ExitVerdict {
    /// `ExitVerdict(code, reason, None, None, False)`.
    #[must_use]
    pub fn rule(code: &str, reason: &str) -> Self {
        Self {
            code: code.to_owned(),
            reason: reason.to_owned(),
            value_points: None,
            marked_loss_inr: None,
            stale: false,
        }
    }
}

/// One open leg as `position_from_rows` reads it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OpenLeg {
    /// Role.
    pub role: LegRole,
    /// Strike.
    pub strike: Decimal,
    /// Filled units (or planned when none).
    pub quantity: i64,
    /// Average fill.
    pub fill_price: Decimal,
    /// Token.
    pub instrument_token: i64,
}

/// `TrackedPosition` with the parts of `OpenPosition` the desk reads.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Tracked {
    /// `op_session.id`.
    pub session_id: i64,
    /// The sleeve.
    pub sleeve: Sleeve,
    /// The structure.
    pub structure: Structure,
    /// Legs.
    pub legs: Vec<OpenLeg>,
    /// Entry premium per unit.
    pub entry_points: Decimal,
    /// When it opened.
    pub opened_at: DateTime<FixedOffset>,
    /// R in ₹.
    pub risk_budget_inr: Decimal,
    /// `UP`/`DOWN` (O2/O3).
    pub direction: Option<super::core::Direction>,
    /// The invalidation range's high.
    pub range_high: Option<Decimal>,
    /// The invalidation range's low.
    pub range_low: Option<Decimal>,
    /// O3-B's half gap.
    pub half_gap: Option<Decimal>,
    /// The spread's width.
    pub width_points: Option<Decimal>,
    /// The exit plan already raised.
    pub exit_plan_id: Option<String>,
}

/// `position_from_rows(head, legs)`.
///
/// # Errors
/// A value Python's constructors refuse.
pub fn position_from_rows(head: &Row, legs: &[Row]) -> Result<Tracked, StoreError> {
    let detail = py::json_obj(col(head, "detail"));
    let gate = match detail.get("gate") {
        Some(PyValue::Map(m)) => m.clone(),
        _ => PyDict::new(),
    };
    let sleeve = sleeve_col(col(head, "sleeve"))?;
    let direction = match detail.get("direction") {
        Some(v) if v.truthy() => {
            let s = v.py_str();
            Some(
                super::core::direction_of(&s)
                    .ok_or_else(|| data(format!("ValueError: '{s}' is not a valid Direction")))?,
            )
        }
        _ => None,
    };
    let (high, low) = if sleeve == Sleeve::O2 {
        (
            py::dec_py(gate.get("or_high")),
            py::dec_py(gate.get("or_low")),
        )
    } else {
        (
            py::dec_py(gate.get("range_high")),
            py::dec_py(gate.get("range_low")),
        )
    };
    let mut open_legs = Vec::with_capacity(legs.len());
    for leg in legs {
        let role_text = text(col(leg, "role"));
        let filled = int(col(leg, "filled_qty"));
        open_legs.push(OpenLeg {
            role: role_of(&role_text)
                .ok_or_else(|| data(format!("ValueError: '{role_text}' is not a valid LegRole")))?,
            strike: py::dec(col(leg, "strike")).unwrap_or(Decimal::ZERO),
            quantity: if filled != 0 {
                filled
            } else {
                int(col(leg, "quantity"))
            },
            fill_price: py::dec(col(leg, "avg_price")).unwrap_or(Decimal::ZERO),
            instrument_token: int(col(leg, "instrument_token")),
        });
    }
    let structure_text = text(col(head, "structure"));
    Ok(Tracked {
        session_id: int(col(head, "session_id")),
        sleeve,
        structure: super::core::structure_of(&structure_text).ok_or_else(|| {
            data(format!(
                "ValueError: '{structure_text}' is not a valid Structure"
            ))
        })?,
        legs: open_legs,
        entry_points: py::dec(col(head, "entry_points")).unwrap_or(Decimal::ZERO),
        opened_at: py::aware(col(head, "opened_at"))
            .ok_or_else(|| data("TypeError: opened_at is not a datetime"))?,
        risk_budget_inr: py::dec(col(head, "risk_budget_inr")).unwrap_or(Decimal::ZERO),
        direction,
        range_high: high,
        range_low: low,
        half_gap: py::dec_py(gate.get("half_gap")),
        width_points: py::dec(col(head, "width_points")),
        exit_plan_id: py::opt_text(col(head, "exit_plan_id")),
    })
}
