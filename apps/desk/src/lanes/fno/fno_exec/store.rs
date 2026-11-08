//! Ports `fno_execute.FoStore` and its rows: the FO book over the screener's `fo_` tables
//! (schema `public`) through the desk's Postgres adapter. Every statement is Python's text,
//! names its schema and its user.

use chrono::{DateTime, FixedOffset, NaiveDate};
use rust_decimal::Decimal;

use baskfy_core::fno::config::FoSleeve;

use crate::lanes::options::py::{self, col, int, text};
use crate::lanes::options::store::{StoreError, t};
use crate::storage::{Conn, DbValue, Param, Row};
use crate::wire::{PyDict, PyValue};

/// `SCHEMA`.
pub const SCHEMA: &str = "public";

/// `FoSleeve(text)`.
#[must_use]
pub fn sleeve_of(text: &str) -> Option<FoSleeve> {
    FoSleeve::ALL.into_iter().find(|s| s.value() == text)
}

fn data(msg: impl Into<String>) -> StoreError {
    StoreError::Data(msg.into())
}

fn sleeve_col(v: &DbValue) -> Result<FoSleeve, StoreError> {
    let s = text(v);
    sleeve_of(&s).ok_or_else(|| data(format!("ValueError: '{s}' is not a valid FoSleeve")))
}

/// `FoLegRow`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FoLegRow {
    /// `fo_leg.id`.
    pub id: i64,
    /// Send order.
    pub entry_seq: i64,
    /// `LONG_PUT`, …, `LONG_FUTURE`.
    pub role: String,
    /// The contract.
    pub tradingsymbol: String,
    /// Kite's token.
    pub instrument_token: i64,
    /// Expiry.
    pub expiry: NaiveDate,
    /// Strike (`None` for a future).
    pub strike: Option<Decimal>,
    /// `CE`/`PE`/`XX`.
    pub option_type: String,
    /// `BUY`/`SELL`.
    pub side: String,
    /// Units.
    pub quantity: i64,
}

/// `FoPlanRow`.
#[derive(Debug, Clone, PartialEq)]
pub struct FoPlanRow {
    /// `fo_plan.id`.
    pub pk: i64,
    /// `fo_plan.plan_id`.
    pub plan_id: String,
    /// The sleeve.
    pub sleeve: FoSleeve,
    /// The underlying.
    pub symbol: String,
    /// The session.
    pub trade_date: NaiveDate,
    /// `IRON_CONDOR` / `FUTURE` / `CREDIT_SPREAD`.
    pub structure: String,
    /// `ENTRY` / `EXIT` / `ROLL` / `ADD`.
    pub kind: String,
    /// The status.
    pub status: String,
    /// Issued.
    pub issued_at: DateTime<FixedOffset>,
    /// The window's end.
    pub expires_at: DateTime<FixedOffset>,
    /// Lots.
    pub lots: i64,
    /// Units per lot.
    pub lot_size: i64,
    /// The entry an exit/roll/add hangs off.
    pub parent_plan_id: Option<String>,
    /// F1's `E - 1`.
    pub hard_exit_date: Option<NaiveDate>,
    /// When confirmed.
    pub confirmed_at: Option<DateTime<FixedOffset>>,
    /// `detail` (jsonb).
    pub detail: PyDict,
    /// Legs in `entry_seq` order.
    pub legs: Vec<FoLegRow>,
}

impl FoPlanRow {
    /// `{lg.role: lg for lg in plan.legs}[role]`.
    #[must_use]
    pub fn leg(&self, role: &str) -> Option<&FoLegRow> {
        self.legs.iter().rev().find(|l| l.role == role)
    }
}

/// `PositionRow`.
#[derive(Debug, Clone, PartialEq)]
pub struct PositionRow {
    /// `fo_position.id`.
    pub id: i64,
    /// The sleeve.
    pub sleeve: FoSleeve,
    /// The underlying.
    pub symbol: String,
    /// The structure.
    pub structure: String,
    /// The entry plan.
    pub entry_plan_id: String,
    /// `legs` (jsonb: `{"legs": [...], "carry": {...}}`).
    pub legs: PyDict,
    /// Lots.
    pub lots: i64,
    /// Units per lot.
    pub lot_size: i64,
    /// F2's entry.
    pub entry_price: Option<Decimal>,
    /// F1/F3's credit.
    pub entry_credit: Option<Decimal>,
    /// The planned max loss.
    pub max_loss_inr: Option<Decimal>,
    /// F2's trigger / F3's level.
    pub stop_price: Option<Decimal>,
    /// The GTT handle.
    pub gtt_id: Option<String>,
    /// F1's `E - 1`.
    pub hard_exit_date: Option<NaiveDate>,
    /// F2's next roll.
    pub next_roll_date: Option<NaiveDate>,
    /// When opened.
    pub opened_at: DateTime<FixedOffset>,
    /// Paper.
    pub simulated: bool,
}

impl PositionRow {
    /// `position.leg_list`.
    #[must_use]
    pub fn leg_list(&self) -> Vec<PyDict> {
        match self.legs.get("legs") {
            Some(PyValue::List(items)) => items
                .iter()
                .filter_map(|x| match x {
                    PyValue::Map(m) => Some(m.clone()),
                    _ => None,
                })
                .collect(),
            _ => Vec::new(),
        }
    }

    /// `position.carry`.
    #[must_use]
    pub fn carry(&self) -> PyDict {
        match self.legs.get("carry") {
            Some(PyValue::Map(m)) => m.clone(),
            _ => PyDict::new(),
        }
    }
}

/// A value as `psycopg` would bind it, from the Python object the FO code builds.
#[derive(Debug, Clone)]
pub enum Val {
    /// `None`.
    None,
    /// `int`.
    Int(i64),
    /// `bool`.
    Bool(bool),
    /// `str`.
    Str(String),
    /// `Decimal`.
    Dec(Decimal),
    /// `date`.
    Date(NaiveDate),
    /// `datetime`.
    At(DateTime<FixedOffset>),
    /// A dict/list, `dumps`-ed (the `detail`/`legs` columns).
    Json(PyValue),
}

impl Val {
    fn param(&self) -> Param {
        match self {
            Self::None => Param::Null,
            Self::Int(i) => Param::Int(*i),
            Self::Bool(b) => Param::Bool(*b),
            Self::Str(s) => Param::Text(s.clone()),
            Self::Dec(d) => Param::Decimal(*d),
            Self::Date(d) => Param::Date(*d),
            Self::At(t) => Param::Aware(*t),
            Self::Json(v) => Param::Text(py::dumps(v)),
        }
    }

    /// An optional `Decimal`.
    #[must_use]
    pub fn dec(d: Option<Decimal>) -> Self {
        d.map_or(Self::None, Self::Dec)
    }

    /// An optional `date`.
    #[must_use]
    pub fn date(d: Option<NaiveDate>) -> Self {
        d.map_or(Self::None, Self::Date)
    }
}

/// `money(value)`.
#[must_use]
pub fn money(value: Decimal) -> Decimal {
    py::q2(value)
}

/// `FoStore`.
pub struct FoStore<'c> {
    /// The connection.
    pub conn: &'c mut Conn,
    /// `user_id`.
    pub user_id: i64,
    /// `schema`.
    pub schema: String,
}

impl<'c> FoStore<'c> {
    /// `FoStore(conn, user_id=, schema=SCHEMA)`.
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

    /// `conn.execute(sql, params).fetchall()`.
    ///
    /// # Errors
    /// The database.
    pub async fn rows(&mut self, sql: &str, params: &[Param]) -> Result<Vec<Row>, StoreError> {
        Ok(self.conn.execute(sql, params).await?.rows)
    }

    /// `.fetchone()`.
    ///
    /// # Errors
    /// The database.
    pub async fn one(&mut self, sql: &str, params: &[Param]) -> Result<Option<Row>, StoreError> {
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
    /// The database.
    pub async fn plan(&mut self, plan_id: &str) -> Result<Option<FoPlanRow>, StoreError> {
        let sql = format!(
            "SELECT * FROM {} WHERE user_id = ? AND plan_id = ?",
            self.t("fo_plan")
        );
        match self
            .one(
                &sql,
                &[Param::Int(self.user_id), Param::Text(plan_id.to_owned())],
            )
            .await?
        {
            None => Ok(None),
            Some(r) => self.plan_of(&r).await.map(Some),
        }
    }

    /// `store._plan(row)`.
    ///
    /// # Errors
    /// The database, or a row Python would refuse.
    pub async fn plan_of(&mut self, row: &Row) -> Result<FoPlanRow, StoreError> {
        let sql = format!(
            "SELECT id, entry_seq, role, tradingsymbol, instrument_token, expiry, strike, \
             option_type, side, quantity FROM {} WHERE plan_id = ? ORDER BY entry_seq",
            self.t("fo_leg")
        );
        let legs = self.rows(&sql, &[Param::Int(int(col(row, "id")))]).await?;
        let mut out = Vec::with_capacity(legs.len());
        for lg in &legs {
            out.push(FoLegRow {
                id: int(col(lg, "id")),
                entry_seq: int(col(lg, "entry_seq")),
                role: text(col(lg, "role")),
                tradingsymbol: text(col(lg, "tradingsymbol")),
                instrument_token: int(col(lg, "instrument_token")),
                expiry: py::opt_day(col(lg, "expiry"))
                    .ok_or_else(|| data("TypeError: expiry is not a date"))?,
                strike: py::px(col(lg, "strike")),
                option_type: text(col(lg, "option_type")),
                side: text(col(lg, "side")),
                quantity: int(col(lg, "quantity")),
            });
        }
        Ok(FoPlanRow {
            pk: int(col(row, "id")),
            plan_id: text(col(row, "plan_id")),
            sleeve: sleeve_col(col(row, "sleeve"))?,
            symbol: text(col(row, "symbol")),
            trade_date: py::opt_day(col(row, "trade_date"))
                .ok_or_else(|| data("TypeError: trade_date is not a date"))?,
            structure: text(col(row, "structure")),
            kind: text(col(row, "kind")),
            status: text(col(row, "status")),
            issued_at: py::aware(col(row, "issued_at"))
                .ok_or_else(|| data("TypeError: issued_at is not a datetime"))?,
            expires_at: py::aware(col(row, "expires_at"))
                .ok_or_else(|| data("TypeError: expires_at is not a datetime"))?,
            lots: int(col(row, "lots")),
            lot_size: int(col(row, "lot_size")),
            parent_plan_id: py::opt_text(col(row, "parent_plan_id")),
            hard_exit_date: py::opt_day(col(row, "hard_exit_date")),
            confirmed_at: if col(row, "confirmed_at").is_null() {
                None
            } else {
                py::aware(col(row, "confirmed_at"))
            },
            detail: py::json_obj(col(row, "detail")),
            legs: out,
        })
    }

    /// `store.insert_plan(head, legs)`: `false` (and nothing written) when the id exists.
    ///
    /// # Errors
    /// The database.
    pub async fn insert_plan(
        &mut self,
        head: &[(&str, Val)],
        legs: &[Vec<(&str, Val)>],
    ) -> Result<bool, StoreError> {
        let mut cols = vec!["user_id"];
        cols.extend(head.iter().map(|(k, _)| *k));
        let mut params = vec![Param::Int(self.user_id)];
        params.extend(head.iter().map(|(_, v)| v.param()));
        let sql = format!(
            "INSERT INTO {} ({}) VALUES ({}) ON CONFLICT (plan_id) DO NOTHING RETURNING id",
            self.t("fo_plan"),
            cols.join(", "),
            vec!["?"; cols.len()].join(", ")
        );
        let Some(row) = self.one(&sql, &params).await? else {
            return Ok(false);
        };
        let plan_pk = int(col(&row, "id"));
        for leg in legs {
            let mut lcols = vec!["user_id", "plan_id"];
            lcols.extend(leg.iter().map(|(k, _)| *k));
            let mut lparams = vec![Param::Int(self.user_id), Param::Int(plan_pk)];
            lparams.extend(leg.iter().map(|(_, v)| v.param()));
            let sql = format!(
                "INSERT INTO {} ({}) VALUES ({})",
                self.t("fo_leg"),
                lcols.join(", "),
                vec!["?"; lcols.len()].join(", ")
            );
            self.conn.execute(&sql, &lparams).await?;
        }
        Ok(true)
    }

    /// `store.set_status(plan_id, status, reason=None, only_from=None)`.
    ///
    /// # Errors
    /// The database.
    pub async fn set_status(
        &mut self,
        plan_id: &str,
        status: &str,
        reason: Option<&str>,
        only_from: &[&str],
    ) -> Result<(), StoreError> {
        let guard = if only_from.is_empty() {
            String::new()
        } else {
            format!(" AND status IN ({})", vec!["?"; only_from.len()].join(", "))
        };
        let sql = format!(
            "UPDATE {} SET status = ?, reason = COALESCE(?, reason) \
             WHERE user_id = ? AND plan_id = ?{guard}",
            self.t("fo_plan")
        );
        let mut params = vec![
            Param::Text(status.to_owned()),
            reason.map_or(Param::Null, |r| Param::Text(r.to_owned())),
            Param::Int(self.user_id),
            Param::Text(plan_id.to_owned()),
        ];
        params.extend(only_from.iter().map(|s| Param::Text((*s).to_owned())));
        self.conn.execute(&sql, &params).await?;
        Ok(())
    }

    /// `store.confirm(plan, at)`: `true` when this call moved it `ISSUED → CONFIRMED`.
    ///
    /// # Errors
    /// The database.
    pub async fn confirm(
        &mut self,
        plan: &FoPlanRow,
        at: DateTime<FixedOffset>,
    ) -> Result<bool, StoreError> {
        let sql = format!(
            "UPDATE {} SET status = 'CONFIRMED', confirmed_at = ? \
             WHERE id = ? AND status = 'ISSUED' RETURNING id",
            self.t("fo_plan")
        );
        Ok(self
            .one(&sql, &[Param::Aware(at), Param::Int(plan.pk)])
            .await?
            .is_some())
    }

    /// `store.merge_detail(plan_id, extra)`.
    ///
    /// # Errors
    /// The database.
    pub async fn merge_detail(&mut self, plan_id: &str, extra: &PyDict) -> Result<(), StoreError> {
        let sql = format!(
            "UPDATE {} SET detail = COALESCE(detail, '{{}}'::jsonb) || ?::jsonb \
             WHERE user_id = ? AND plan_id = ?",
            self.t("fo_plan")
        );
        self.conn
            .execute(
                &sql,
                &[
                    Param::Text(py::dumps(&PyValue::Map(extra.clone()))),
                    Param::Int(self.user_id),
                    Param::Text(plan_id.to_owned()),
                ],
            )
            .await?;
        Ok(())
    }

    /// `store.add_violation(plan_id, code, message, at)`: append-only.
    ///
    /// # Errors
    /// The database.
    pub async fn add_violation(
        &mut self,
        plan_id: &str,
        code: &str,
        message: &str,
        at: DateTime<FixedOffset>,
    ) -> Result<(), StoreError> {
        let item = PyValue::List(vec![PyValue::Map(crate::wire::dict([
            ("code", PyValue::Str(code.to_owned())),
            ("message", PyValue::Str(message.to_owned())),
            ("at", PyValue::Str(py::iso_aware(&at))),
        ]))]);
        let sql = format!(
            "UPDATE {} SET detail = COALESCE(detail, '{{}}'::jsonb) || \
             jsonb_build_object('violations', COALESCE(detail->'violations', '[]'::jsonb) || \
             ?::jsonb) WHERE user_id = ? AND plan_id = ?",
            self.t("fo_plan")
        );
        self.conn
            .execute(
                &sql,
                &[
                    Param::Text(py::dumps(&item)),
                    Param::Int(self.user_id),
                    Param::Text(plan_id.to_owned()),
                ],
            )
            .await?;
        Ok(())
    }

    /// `store.next_attempt(plan_pk, key)`: the persisted attempt number for `key`.
    ///
    /// # Errors
    /// The database.
    pub async fn next_attempt(&mut self, plan_pk: i64, key: &str) -> Result<i64, StoreError> {
        let sql = format!(
            "UPDATE {} SET detail = COALESCE(detail, '{{}}'::jsonb) || \
             jsonb_build_object('attempts', COALESCE(detail->'attempts', '{{}}'::jsonb) || \
             jsonb_build_object(?::text, COALESCE((detail->'attempts'->>?)::int, 0) + 1)) \
             WHERE id = ? RETURNING (detail->'attempts'->>?)::int AS n",
            self.t("fo_plan")
        );
        let row = self
            .one(
                &sql,
                &[
                    Param::Text(key.to_owned()),
                    Param::Text(key.to_owned()),
                    Param::Int(plan_pk),
                    Param::Text(key.to_owned()),
                ],
            )
            .await?
            .ok_or_else(|| data("TypeError: 'NoneType' object is not subscriptable"))?;
        Ok(int(col(&row, "n")))
    }

    /// `store.lapse_expired(now)`.
    ///
    /// # Errors
    /// The database.
    pub async fn lapse_expired(
        &mut self,
        now: DateTime<FixedOffset>,
    ) -> Result<Vec<String>, StoreError> {
        let sql = format!(
            "UPDATE {} SET status = 'LAPSED', reason = ? WHERE user_id = ? \
             AND kind = 'ENTRY' AND status = 'ISSUED' AND expires_at <= ? RETURNING plan_id",
            self.t("fo_plan")
        );
        let rows = self
            .rows(
                &sql,
                &[
                    Param::Text(
                        "not confirmed inside its window; a missed window is not retried on a \
                         later day (04 §1)"
                            .to_owned(),
                    ),
                    Param::Int(self.user_id),
                    Param::Aware(now),
                ],
            )
            .await?;
        Ok(rows.iter().map(|r| text(col(r, "plan_id"))).collect())
    }

    /// `store.pending_actions()`.
    ///
    /// # Errors
    /// The database.
    pub async fn pending_actions(&mut self) -> Result<Vec<FoPlanRow>, StoreError> {
        let sql = format!(
            "SELECT p.* FROM {} p WHERE p.user_id = ? AND (\
             (p.kind = 'ROLL' AND p.status IN ('ISSUED', 'CONFIRMED', 'FILLING')) OR \
             (p.kind = 'EXIT' AND p.status IN ('CONFIRMED', 'FILLING'))) ORDER BY p.id",
            self.t("fo_plan")
        );
        let rows = self.rows(&sql, &[Param::Int(self.user_id)]).await?;
        let mut out = Vec::with_capacity(rows.len());
        for r in &rows {
            out.push(self.plan_of(r).await?);
        }
        Ok(out)
    }

    /// `store.write_fill(...)`.
    ///
    /// # Errors
    /// The database.
    #[allow(clippy::too_many_arguments)]
    pub async fn write_fill(
        &mut self,
        leg: &FoLegRow,
        position_id: Option<i64>,
        client_id: &str,
        at: DateTime<FixedOffset>,
        side: &str,
        quantity: i64,
        price: Decimal,
        detail: &PyDict,
    ) -> Result<(), StoreError> {
        let sql = format!(
            "INSERT INTO {} (user_id, leg_id, position_id, client_id, filled_at, \
             side, quantity, price, simulated, sim_method, detail) \
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, true, 'DEPTH_LADDER', ?)",
            self.t("fo_fill")
        );
        self.conn
            .execute(
                &sql,
                &[
                    Param::Int(self.user_id),
                    Param::Int(leg.id),
                    position_id.map_or(Param::Null, Param::Int),
                    Param::Text(client_id.to_owned()),
                    Param::Aware(at),
                    Param::Text(side.to_owned()),
                    Param::Int(quantity),
                    Param::Decimal(money(price)),
                    Param::Text(py::dumps(&PyValue::Map(detail.clone()))),
                ],
            )
            .await?;
        Ok(())
    }

    /// `store.settle_legs(plan)`: each leg's filled quantity and average from `fo_fill`.
    ///
    /// # Errors
    /// The database.
    pub async fn settle_legs(&mut self, plan: &FoPlanRow) -> Result<(), StoreError> {
        for leg in &plan.legs {
            let sql = format!(
                "SELECT COALESCE(SUM(quantity), 0) AS q, SUM(quantity * price) AS v \
                 FROM {} WHERE leg_id = ? AND side = ?",
                self.t("fo_fill")
            );
            let row = self
                .one(&sql, &[Param::Int(leg.id), Param::Text(leg.side.clone())])
                .await?
                .unwrap_or_default();
            let filled = int(col(&row, "q"));
            let avg = if filled == 0 {
                None
            } else {
                py::dec(col(&row, "v"))
                    .and_then(|v| py::div(v, Decimal::from(filled)).ok())
                    .map(money)
            };
            let status = if filled >= leg.quantity {
                "FILLED"
            } else if filled != 0 {
                "PARTIAL"
            } else {
                "CANCELLED"
            };
            let sql = format!(
                "UPDATE {} SET filled_qty = ?, avg_price = ?, status = ? WHERE id = ?",
                self.t("fo_leg")
            );
            self.conn
                .execute(
                    &sql,
                    &[
                        Param::Int(filled.min(leg.quantity)),
                        py::pdec(avg),
                        Param::Text(status.to_owned()),
                        Param::Int(leg.id),
                    ],
                )
                .await?;
        }
        Ok(())
    }

    /// `store.attach_fills(plan, position_id)`.
    ///
    /// # Errors
    /// The database.
    pub async fn attach_fills(
        &mut self,
        plan: &FoPlanRow,
        position_id: i64,
    ) -> Result<(), StoreError> {
        let ids: Vec<i64> = plan.legs.iter().map(|l| l.id).collect();
        let sql = format!(
            "UPDATE {} SET position_id = ? WHERE leg_id = ANY({}) AND position_id IS NULL",
            self.t("fo_fill"),
            py::int_array_sql(&ids)
        );
        self.conn.execute(&sql, &[Param::Int(position_id)]).await?;
        Ok(())
    }

    async fn net(&mut self, sql: &str, key: i64) -> Result<Vec<(String, i64)>, StoreError> {
        let rows = self.rows(sql, &[Param::Int(key)]).await?;
        Ok(rows
            .iter()
            .map(|r| (text(col(r, "tradingsymbol")), int(col(r, "n"))))
            .collect())
    }

    /// `store.net_by_symbol(position_id)`.
    ///
    /// # Errors
    /// The database.
    pub async fn net_by_symbol(
        &mut self,
        position_id: i64,
    ) -> Result<Vec<(String, i64)>, StoreError> {
        let sql = format!(
            "SELECT l.tradingsymbol, COALESCE(SUM(CASE WHEN f.side = 'BUY' THEN f.quantity \
             ELSE -f.quantity END), 0) AS n FROM {} f \
             JOIN {} l ON l.id = f.leg_id WHERE f.position_id = ? \
             GROUP BY l.tradingsymbol",
            self.t("fo_fill"),
            self.t("fo_leg")
        );
        self.net(&sql, position_id).await
    }

    /// `store.open_position(row)`: its id.
    ///
    /// # Errors
    /// The database.
    pub async fn open_position(&mut self, row: &[(&str, Val)]) -> Result<i64, StoreError> {
        let mut cols = vec!["user_id"];
        cols.extend(row.iter().map(|(k, _)| *k));
        let mut params = vec![Param::Int(self.user_id)];
        params.extend(row.iter().map(|(_, v)| v.param()));
        let sql = format!(
            "INSERT INTO {} ({}) VALUES ({}) RETURNING id",
            self.t("fo_position"),
            cols.join(", "),
            vec!["?"; cols.len()].join(", ")
        );
        let out = self
            .one(&sql, &params)
            .await?
            .ok_or_else(|| data("TypeError: 'NoneType' object is not subscriptable"))?;
        Ok(int(col(&out, "id")))
    }

    fn position_of(r: &Row) -> Result<PositionRow, StoreError> {
        Ok(PositionRow {
            id: int(col(r, "id")),
            sleeve: sleeve_col(col(r, "sleeve"))?,
            symbol: text(col(r, "symbol")),
            structure: text(col(r, "structure")),
            entry_plan_id: text(col(r, "entry_plan_id")),
            legs: py::json_obj(col(r, "legs")),
            lots: int(col(r, "lots")),
            lot_size: int(col(r, "lot_size")),
            entry_price: py::px(col(r, "entry_price")),
            entry_credit: py::px(col(r, "entry_credit")),
            max_loss_inr: py::px(col(r, "max_loss_inr")),
            stop_price: py::px(col(r, "stop_price")),
            gtt_id: py::opt_text(col(r, "gtt_id")),
            hard_exit_date: py::opt_day(col(r, "hard_exit_date")),
            next_roll_date: py::opt_day(col(r, "next_roll_date")),
            opened_at: py::aware(col(r, "opened_at"))
                .ok_or_else(|| data("TypeError: opened_at is not a datetime"))?,
            simulated: py::truthy(col(r, "simulated")),
        })
    }

    /// `store.open_positions()`.
    ///
    /// # Errors
    /// The database.
    pub async fn open_positions(&mut self) -> Result<Vec<PositionRow>, StoreError> {
        let sql = format!(
            "SELECT * FROM {} WHERE user_id = ? AND closed_at IS NULL ORDER BY id",
            self.t("fo_position")
        );
        let rows = self.rows(&sql, &[Param::Int(self.user_id)]).await?;
        rows.iter().map(Self::position_of).collect()
    }

    /// `store.position(position_id)`.
    ///
    /// # Errors
    /// The database.
    pub async fn position(&mut self, position_id: i64) -> Result<Option<PositionRow>, StoreError> {
        let sql = format!(
            "SELECT * FROM {} WHERE user_id = ? AND id = ?",
            self.t("fo_position")
        );
        self.one(&sql, &[Param::Int(self.user_id), Param::Int(position_id)])
            .await?
            .as_ref()
            .map(Self::position_of)
            .transpose()
    }

    /// `store.update_position(position_id, **values)`.
    ///
    /// # Errors
    /// The database.
    pub async fn update_position(
        &mut self,
        position_id: i64,
        values: &[(&str, Val)],
    ) -> Result<(), StoreError> {
        let sets: Vec<String> = values.iter().map(|(k, _)| format!("{k} = ?")).collect();
        let sql = format!(
            "UPDATE {} SET {} WHERE user_id = ? AND id = ?",
            self.t("fo_position"),
            sets.join(", ")
        );
        let mut params: Vec<Param> = values.iter().map(|(_, v)| v.param()).collect();
        params.push(Param::Int(self.user_id));
        params.push(Param::Int(position_id));
        self.conn.execute(&sql, &params).await?;
        Ok(())
    }

    /// `store.close_position(position_id, reason, at)`.
    ///
    /// # Errors
    /// The database.
    pub async fn close_position(
        &mut self,
        position_id: i64,
        reason: &str,
        at: DateTime<FixedOffset>,
    ) -> Result<(), StoreError> {
        let sql = format!(
            "UPDATE {} SET closed_at = ?, closed_reason = ? \
             WHERE user_id = ? AND id = ? AND closed_at IS NULL",
            self.t("fo_position")
        );
        let short: String = reason.chars().take(32).collect();
        self.conn
            .execute(
                &sql,
                &[
                    Param::Aware(at),
                    Param::Text(short),
                    Param::Int(self.user_id),
                    Param::Int(position_id),
                ],
            )
            .await?;
        Ok(())
    }

    /// `store.write_mark(...)`: `true` on the session's first mark.
    ///
    /// # Errors
    /// The database.
    pub async fn write_mark(
        &mut self,
        position_id: i64,
        day: NaiveDate,
        mark_points: Decimal,
        pnl_inr: Decimal,
        stop_price: Option<Decimal>,
        detail: &PyDict,
    ) -> Result<bool, StoreError> {
        let sql = format!(
            "INSERT INTO {} (position_id, trade_date, user_id, mark_points, \
             pnl_inr, stop_price, detail) VALUES (?, ?, ?, ?, ?, ?, ?) \
             ON CONFLICT (position_id, trade_date) DO NOTHING RETURNING position_id",
            self.t("fo_mark")
        );
        Ok(self
            .one(
                &sql,
                &[
                    Param::Int(position_id),
                    Param::Date(day),
                    Param::Int(self.user_id),
                    Param::Decimal(money(mark_points)),
                    Param::Decimal(money(pnl_inr)),
                    py::pdec(stop_price.map(money)),
                    Param::Text(py::dumps(&PyValue::Map(detail.clone()))),
                ],
            )
            .await?
            .is_some())
    }

    /// `store.marked_days(position_id)`.
    ///
    /// # Errors
    /// The database.
    pub async fn marked_days(&mut self, position_id: i64) -> Result<Vec<NaiveDate>, StoreError> {
        let sql = format!(
            "SELECT trade_date FROM {} WHERE position_id = ?",
            self.t("fo_mark")
        );
        Ok(self
            .rows(&sql, &[Param::Int(position_id)])
            .await?
            .iter()
            .filter_map(|r| py::opt_day(col(r, "trade_date")))
            .collect())
    }
}

/// `FoQuote`: a contract's live book.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct FoQuote {
    /// Best bid.
    pub bid: Option<Decimal>,
    /// Best ask.
    pub ask: Option<Decimal>,
    /// Bid ladder.
    pub bids: Vec<baskfy_core::options::execution::Level>,
    /// Ask ladder.
    pub asks: Vec<baskfy_core::options::execution::Level>,
    /// Last price.
    pub last: Option<Decimal>,
    /// Open interest.
    pub oi: Option<i64>,
}

impl FoQuote {
    /// `two_sided`.
    #[must_use]
    pub fn two_sided(&self) -> bool {
        matches!((self.bid, self.ask), (Some(b), Some(a)) if b > Decimal::ZERO && a > Decimal::ZERO)
    }

    /// `mid`: `(bid + ask) / 2`.
    #[must_use]
    pub fn mid(&self) -> Option<Decimal> {
        if !self.two_sided() {
            return None;
        }
        let (b, a) = (self.bid?, self.ask?);
        py::div(b + a, Decimal::from(2)).ok()
    }
}
