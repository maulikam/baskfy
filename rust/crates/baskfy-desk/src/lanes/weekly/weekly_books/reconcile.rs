//! `app/reconcile.py` — the LV2 reconciler: the broker's order book, GTT list and holdings
//! against every sleeve's book.
//!
//! One pass reads the order book **once** and hands each sleeve's open order to that sleeve's own
//! idempotent `on_order_update`; gives a dead order's unfilled remainder back to the risk ledger;
//! reads the GTT list and holdings **once each** and judges every open position's protection
//! (`NAKED`, `GTT_MISSING`, `GTT_OVERSIZED`, `GTT_UNDERSIZED`, `GTT_TRIGGERED_UNFILLED`,
//! `EXTERNAL_EXIT`), recording findings and resolving what a pass could observe and did not see.
//! A read that failed resolves nothing of its kind — an unreadable broker is not a clean bill.
//!
//! **It never places a buy.** Every broker mutation a pass causes goes through the sleeve's
//! handler and the gateway (law 2); this module holds no Kite writer, only the three reads of
//! [`OrderBook`].
//!
//! **Not here (loud):** `twt_hooks`, `vbt_hooks`, `swing_hooks` and `run_pass_from_desk` wire the
//! sleeves' stores and gateways; each sleeve lane implements [`SleeveHooks`] over its own
//! `on_order_update`, and lane `session` (the supervisor) runs the pass.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};

use chrono::{DateTime, FixedOffset};
use rust_decimal::Decimal;

use super::{pyi, pys};
use crate::clock::ist;
use crate::storage::{Conn, DbValue, Param, StorageError};
use crate::wire::{PyDict, PyValue};

/// Kite's one fill state.
pub const ORDER_COMPLETE: &str = "COMPLETE";
/// Statuses the broker will not fill any further.
pub const ORDER_DEAD_STATUSES: &[&str] = &["REJECTED", "CANCELLED", "CANCELLED AMO"];
/// A resting GTT.
pub const GTT_ACTIVE: &str = "active";
/// A GTT that fired (a LIMIT order was placed — not a fill).
pub const GTT_TRIGGERED: &str = "triggered";
/// GTT statuses that mean gone.
pub const GTT_GONE_STATUSES: &[&str] = &["cancelled", "deleted", "expired", "disabled", "rejected"];
/// A simulated GTT id, as the gateway mints it.
pub const SIMULATED_GTT_PREFIX: &str = "DRY-";

/// No resting stop.
pub const NAKED: &str = "NAKED";
/// The stop is not in the broker's list (or is gone).
pub const GTT_MISSING: &str = "GTT_MISSING";
/// The stop sells more than is held.
pub const GTT_OVERSIZED: &str = "GTT_OVERSIZED";
/// The stop sells less than is held.
pub const GTT_UNDERSIZED: &str = "GTT_UNDERSIZED";
/// The stop fired and the position is still open.
pub const GTT_TRIGGERED_UNFILLED: &str = "GTT_TRIGGERED_UNFILLED";
/// Shares left the broker's book outside the desk.
pub const EXTERNAL_EXIT: &str = "EXTERNAL_EXIT";
/// The broker refused the stop.
pub const STOP_REJECTED: &str = "STOP_REJECTED";
/// Every kind.
pub const ISSUE_KINDS: &[&str] = &[
    NAKED,
    GTT_MISSING,
    GTT_OVERSIZED,
    GTT_UNDERSIZED,
    GTT_TRIGGERED_UNFILLED,
    EXTERNAL_EXIT,
    STOP_REJECTED,
];
/// The kinds a pass can judge only with the GTT list.
pub const GTT_KINDS: &[&str] = &[
    GTT_MISSING,
    GTT_OVERSIZED,
    GTT_UNDERSIZED,
    GTT_TRIGGERED_UNFILLED,
];
/// The kinds a pass can judge only with the holdings.
pub const HOLDINGS_KINDS: &[&str] = &[EXTERNAL_EXIT];
/// The sleeves the desk reconciles.
pub const SLEEVES: &[&str] = &["swing", "twt", "vbt"];

/// A failure as Python's handler records it: `"<ExceptionType>: <message>"`.
pub type HookError = String;

/// The broker's three reads (the desk's `Kite` wrapper: `orders`, `get_gtts`, `holdings` — the
/// last already summing `quantity + t1_quantity + collateral_quantity`). Nothing here writes.
#[async_trait::async_trait]
pub trait OrderBook: Send + Sync {
    /// `orders()`.
    async fn orders(&self) -> Result<Vec<PyValue>, HookError>;
    /// `get_gtts()`.
    async fn get_gtts(&self) -> Result<Vec<PyValue>, HookError>;
    /// `holdings()`.
    async fn holdings(&self) -> Result<Vec<PyDict>, HookError>;
}

/// One thing wrong with one position's protection.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Issue {
    /// `sleeve`.
    pub sleeve: String,
    /// `position_id`.
    pub position_id: i64,
    /// `symbol`.
    pub symbol: String,
    /// `kind`.
    pub kind: String,
    /// `detail`.
    pub detail: String,
}

/// An open issue as the store returns it.
#[derive(Debug, Clone, PartialEq)]
pub struct OpenIssue {
    /// `id`.
    pub id: i64,
    /// `sleeve`.
    pub sleeve: String,
    /// `position_id`.
    pub position_id: i64,
    /// `symbol`.
    pub symbol: String,
    /// `kind`.
    pub kind: String,
    /// `detail`.
    pub detail: String,
    /// `seen_at` (as stored).
    pub seen_at: PyValue,
}

/// Where findings live.
#[async_trait::async_trait]
pub trait IssueStore: Send {
    /// Open issues (one sleeve, or all).
    async fn open_issues(&mut self, sleeve: Option<&str>) -> Result<Vec<OpenIssue>, HookError>;
    /// Record (or refresh) one finding.
    async fn record(&mut self, issue: &Issue, now: DateTime<FixedOffset>) -> Result<(), HookError>;
    /// Resolve every open issue of `sleeve` whose kind is in `kinds` and whose
    /// `(position_id, kind)` is not in `keep`. Returns how many.
    async fn resolve_except(
        &mut self,
        sleeve: &str,
        kinds: &BTreeSet<String>,
        keep: &HashSet<(i64, String)>,
        now: DateTime<FixedOffset>,
    ) -> Result<i64, HookError>;
}

/// One sleeve's book, as the reconciler sees it (`SleeveHooks`). `open_orders` rows carry
/// `broker_order_id`, `symbol`, `quantity`, `filled_quantity`, `reference_price`; `open_positions`
/// rows `id`, `symbol`, `quantity_open`, `gtt_id`, `simulated`.
#[async_trait::async_trait]
pub trait SleeveHooks: Send + Sync {
    /// The sleeve's name.
    fn name(&self) -> &str;
    /// `open_orders()`.
    async fn open_orders(&self) -> Result<Vec<PyDict>, HookError>;
    /// The sleeve's idempotent handler over one order-book row; `Some` when it applied something.
    async fn on_order_update(&self, payload: &PyDict) -> Result<Option<PyValue>, HookError>;
    /// `open_positions()`.
    async fn open_positions(&self) -> Result<Vec<PyDict>, HookError>;
    /// Whether the sleeve has a risk manager (`risk is not None`).
    fn has_risk(&self) -> bool;
    /// `risk.release(symbol, value)`.
    fn release(&self, symbol: &str, value: f64);
}

/// One Kite GTT, as the reconciler reads it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GttRow {
    /// `gtt_id`.
    pub gtt_id: String,
    /// `status` (lower-cased).
    pub status: String,
    /// `symbol`.
    pub symbol: String,
    /// The sell leg's quantity.
    pub quantity: Option<i64>,
    /// The first trigger value.
    pub trigger: Option<Decimal>,
}

/// What one pass saw and did.
#[derive(Debug, Clone, PartialEq)]
pub struct ReconcileRun {
    /// `at`.
    pub at: DateTime<FixedOffset>,
    /// Orders found in the book.
    pub seen: i64,
    /// Updates a handler applied.
    pub applied: i64,
    /// Dead orders.
    pub dead: i64,
    /// Remainders released.
    pub released: i64,
    /// Orders the book did not carry.
    pub unseen: i64,
    /// Findings.
    pub issues: Vec<Issue>,
    /// Issues resolved.
    pub resolved: i64,
    /// Reads and handlers that failed.
    pub errors: Vec<String>,
}

impl ReconcileRun {
    /// An empty run at `at`.
    #[must_use]
    pub const fn new(at: DateTime<FixedOffset>) -> Self {
        Self {
            at,
            seen: 0,
            applied: 0,
            dead: 0,
            released: 0,
            unseen: 0,
            issues: Vec::new(),
            resolved: 0,
            errors: Vec::new(),
        }
    }

    /// The one-line summary the heartbeat carries.
    #[must_use]
    pub fn line(&self) -> String {
        let mut kinds: BTreeMap<&str, i64> = BTreeMap::new();
        for i in &self.issues {
            *kinds.entry(i.kind.as_str()).or_insert(0) += 1;
        }
        let found = if kinds.is_empty() {
            "none".to_owned()
        } else {
            kinds
                .iter()
                .map(|(k, v)| format!("{k} {v}"))
                .collect::<Vec<_>>()
                .join(", ")
        };
        let mut out = format!(
            "reconcile {}: {} order(s) seen, {} applied, {} dead, {} released, {} not in the \
             book; issues: {found}; {} resolved",
            self.at.with_timezone(&ist()).format("%H:%M:%S"),
            self.seen,
            self.applied,
            self.dead,
            self.released,
            self.unseen,
            self.resolved
        );
        if !self.errors.is_empty() {
            out.push_str("; errors: ");
            out.push_str(&self.errors.join(" | "));
        }
        out
    }

    /// `ok`: no errors.
    #[must_use]
    pub fn ok(&self) -> bool {
        self.errors.is_empty()
    }
}

/// `str(Decimal(str(x)))` for a trigger value.
fn decimal_of(v: &PyValue) -> Option<Decimal> {
    let text = match v {
        PyValue::Float(f) => baskfy_providers::py::repr_float(*f),
        other => other.py_str(),
    };
    text.parse::<Decimal>()
        .ok()
        .or_else(|| Decimal::from_scientific(&text).ok())
}

/// `gtt_index(raw)`: Kite's GTT rows by id.
#[must_use]
pub fn gtt_index(raw: &[PyValue]) -> HashMap<String, GttRow> {
    let empty = PyDict::new();
    let mut out = HashMap::new();
    for t in raw {
        let PyValue::Map(trigger) = t else { continue };
        let Some(id) = trigger.get("id").filter(|v| !matches!(v, PyValue::None)) else {
            continue;
        };
        let condition = match trigger.get("condition") {
            Some(PyValue::Map(m)) => m,
            _ => &empty,
        };
        let values = match condition.get("trigger_values") {
            Some(PyValue::List(l)) => l.clone(),
            _ => Vec::new(),
        };
        let legs = match trigger.get("orders") {
            Some(PyValue::List(l)) => l.clone(),
            _ => Vec::new(),
        };
        let mut quantity = None;
        for leg in &legs {
            let PyValue::Map(leg) = leg else { continue };
            let tt = match leg.get("transaction_type") {
                Some(v) if v.truthy() => v.py_str(),
                _ => "SELL".to_owned(),
            };
            if tt.to_uppercase() == "SELL" {
                quantity = Some(pyi(leg.get("quantity")));
                break;
            }
        }
        let gtt_id = id.py_str();
        out.insert(
            gtt_id.clone(),
            GttRow {
                gtt_id,
                status: pys(trigger.get("status")).to_lowercase(),
                symbol: pys(condition.get("tradingsymbol")),
                quantity,
                trigger: values.first().and_then(decimal_of),
            },
        );
    }
    out
}

/// `holdings_index(raw)`: the desk's holdings rows (`quantity` already the total) by symbol.
#[must_use]
pub fn holdings_index(raw: &[PyDict]) -> HashMap<String, i64> {
    raw.iter()
        .filter(|r| r.get("symbol").is_some_and(PyValue::truthy))
        .map(|r| (pys(r.get("symbol")), pyi(r.get("quantity"))))
        .collect()
}

/// `reconcile_once(book, sleeves, now=, issues=)`: one pass over every sleeve. Never fails for a
/// broker that will not answer: each read is tried once, a failure is recorded on the run.
pub async fn reconcile_once(
    book: &dyn OrderBook,
    sleeves: &[&dyn SleeveHooks],
    now: DateTime<FixedOffset>,
    issues: &mut dyn IssueStore,
) -> ReconcileRun {
    let mut run = ReconcileRun::new(now);
    reconcile_orders(book, sleeves, &mut run).await;
    judge_protection(book, sleeves, &mut run, now, issues).await;
    run
}

async fn reconcile_orders(
    book: &dyn OrderBook,
    sleeves: &[&dyn SleeveHooks],
    run: &mut ReconcileRun,
) {
    let by_id: HashMap<String, PyDict> = match book.orders().await {
        Ok(rows) => rows
            .into_iter()
            .filter_map(|r| match r {
                PyValue::Map(m) if m.get("order_id").is_some_and(PyValue::truthy) => Some((
                    m.get("order_id").map_or_else(String::new, PyValue::py_str),
                    m,
                )),
                _ => None,
            })
            .collect(),
        Err(e) => {
            run.errors.push(format!("orders: {e}"));
            return;
        }
    };
    for sleeve in sleeves {
        let rows = match sleeve.open_orders().await {
            Ok(rows) => rows,
            Err(e) => {
                run.errors
                    .push(format!("{} open_orders: {e}", sleeve.name()));
                continue;
            }
        };
        for row in rows {
            let broker_id = pys(row.get("broker_order_id"));
            if broker_id.is_empty() {
                continue;
            }
            let Some(payload) = by_id.get(&broker_id) else {
                run.unseen += 1;
                continue;
            };
            run.seen += 1;
            match sleeve.on_order_update(payload).await {
                Err(e) => {
                    run.errors
                        .push(format!("{} {broker_id}: {e}", sleeve.name()));
                    continue;
                }
                Ok(Some(_)) => run.applied += 1,
                Ok(None) => {}
            }
            let status = pys(payload.get("status")).to_uppercase();
            if ORDER_DEAD_STATUSES.contains(&status.as_str()) {
                run.dead += 1;
                if release_remainder(*sleeve, &row, payload) {
                    run.released += 1;
                }
            }
        }
    }
}

/// `_release_remainder`: give the unfilled part of a dead order back to the risk ledger.
fn release_remainder(sleeve: &dyn SleeveHooks, row: &PyDict, payload: &PyDict) -> bool {
    if !sleeve.has_risk() {
        return false;
    }
    let remainder = pyi(row.get("quantity")) - pyi(payload.get("filled_quantity"));
    let reference = match row.get("reference_price") {
        None | Some(PyValue::None) => return false,
        Some(v) => v,
    };
    if remainder <= 0 {
        return false;
    }
    let Some(reference) = decimal_of(reference) else {
        return false;
    };
    let value: f64 = (reference * Decimal::from(remainder))
        .to_string()
        .parse()
        .unwrap_or(0.0);
    if value <= 0.0 {
        return false;
    }
    sleeve.release(&pys(row.get("symbol")), value);
    true
}

fn position_symbol(p: &PyDict) -> String {
    let s = pys(p.get("symbol"));
    if s.is_empty() {
        format!(
            "instrument:{}",
            p.get("instrument_id").unwrap_or(&PyValue::None).py_str()
        )
    } else {
        s
    }
}

async fn judge_protection(
    book: &dyn OrderBook,
    sleeves: &[&dyn SleeveHooks],
    run: &mut ReconcileRun,
    now: DateTime<FixedOffset>,
    issues: &mut dyn IssueStore,
) {
    let gtts = match book.get_gtts().await {
        Ok(raw) => Some(gtt_index(&raw)),
        Err(e) => {
            run.errors.push(format!("gtts: {e}"));
            None
        }
    };
    let held = match book.holdings().await {
        Ok(raw) => Some(holdings_index(&raw)),
        Err(e) => {
            run.errors.push(format!("holdings: {e}"));
            None
        }
    };
    let mut positions: Vec<(String, Vec<PyDict>)> = Vec::new();
    for sleeve in sleeves {
        match sleeve.open_positions().await {
            Ok(rows) => positions.push((
                sleeve.name().to_owned(),
                rows.into_iter()
                    .filter(|p| pyi(p.get("quantity_open")) > 0)
                    .collect(),
            )),
            Err(e) => run
                .errors
                .push(format!("{} open_positions: {e}", sleeve.name())),
        }
    }
    // Every sleeve's real shares in a name, together: the broker holds one book.
    let mut recorded: HashMap<String, i64> = HashMap::new();
    for (_, rows) in &positions {
        for p in rows {
            if !p.get("simulated").is_some_and(PyValue::truthy) {
                *recorded.entry(position_symbol(p)).or_insert(0) += pyi(p.get("quantity_open"));
            }
        }
    }
    for sleeve in sleeves {
        let Some((_, rows)) = positions.iter().find(|(n, _)| n == sleeve.name()) else {
            continue;
        };
        let found: Vec<Issue> = rows
            .iter()
            .flat_map(|p| judge_position(sleeve.name(), p, gtts.as_ref(), held.as_ref(), &recorded))
            .collect();
        let mut observable: BTreeSet<String> =
            ISSUE_KINDS.iter().map(|k| (*k).to_owned()).collect();
        if gtts.is_none() {
            for k in GTT_KINDS {
                observable.remove(*k);
            }
        }
        if held.is_none() {
            for k in HOLDINGS_KINDS {
                observable.remove(*k);
            }
        }
        for issue in &found {
            if let Err(e) = issues.record(issue, now).await {
                run.errors.push(format!("{} record: {e}", sleeve.name()));
            }
        }
        let keep: HashSet<(i64, String)> = found
            .iter()
            .map(|i| (i.position_id, i.kind.clone()))
            .collect();
        match issues
            .resolve_except(sleeve.name(), &observable, &keep, now)
            .await
        {
            Ok(n) => run.resolved += n,
            Err(e) => run.errors.push(format!("{} resolve: {e}", sleeve.name())),
        }
        run.issues.extend(found);
    }
}

fn judge_position(
    sleeve: &str,
    position: &PyDict,
    gtts: Option<&HashMap<String, GttRow>>,
    held: Option<&HashMap<String, i64>>,
    recorded: &HashMap<String, i64>,
) -> Vec<Issue> {
    let symbol = position_symbol(position);
    let position_id = pyi(position.get("id"));
    let open_qty = pyi(position.get("quantity_open"));
    let gtt_id = position
        .get("gtt_id")
        .filter(|v| !matches!(v, PyValue::None));
    let mut found = Vec::new();
    let mut issue = |kind: &str, detail: String| {
        found.push(Issue {
            sleeve: sleeve.to_owned(),
            position_id,
            symbol: symbol.clone(),
            kind: kind.to_owned(),
            detail,
        });
    };
    match gtt_id {
        None => issue(
            NAKED,
            format!("{open_qty} share(s) held with no resting stop"),
        ),
        Some(id) => {
            let id = id.py_str();
            if let (Some(gtts), false) = (gtts, id.starts_with(SIMULATED_GTT_PREFIX)) {
                let row = gtts.get(&id);
                match row {
                    None => issue(
                        GTT_MISSING,
                        format!(
                            "GTT {id} is not in the broker's list; {open_qty} share(s) unprotected"
                        ),
                    ),
                    Some(r) if GTT_GONE_STATUSES.contains(&r.status.as_str()) => issue(
                        GTT_MISSING,
                        format!("GTT {id} is {}; {open_qty} share(s) unprotected", r.status),
                    ),
                    Some(r) if r.status == GTT_TRIGGERED => {
                        let where_ = held
                            .and_then(|h| h.get(&symbol))
                            .map_or_else(String::new, |s| format!("; the broker still shows {s}"));
                        issue(
                            GTT_TRIGGERED_UNFILLED,
                            format!(
                                "GTT {id} fired and the position is still open for {open_qty}{where_} — \
                                 a trigger is not a fill; check the LIMIT order it placed"
                            ),
                        );
                    }
                    Some(GttRow {
                        quantity: Some(q), ..
                    }) if *q > open_qty => issue(
                        GTT_OVERSIZED,
                        format!(
                            "GTT {id} sells {q} against {open_qty} held — a residual stop would \
                             sell what is not there"
                        ),
                    ),
                    Some(GttRow {
                        quantity: Some(q), ..
                    }) if *q < open_qty => issue(
                        GTT_UNDERSIZED,
                        format!(
                            "GTT {id} sells {q} against {open_qty} held — {} share(s) unprotected",
                            open_qty - q
                        ),
                    ),
                    Some(_) => {}
                }
            }
        }
    }
    if let (Some(held), false) = (held, position.get("simulated").is_some_and(PyValue::truthy)) {
        let have = held.get(&symbol).copied().unwrap_or(0);
        let total = recorded.get(&symbol).copied().unwrap_or(0);
        if have < total {
            issue(
                EXTERNAL_EXIT,
                format!(
                    "the broker holds {have} {symbol} and the sleeves record {total} — {} \
                     share(s) left the book outside the desk",
                    total - have
                ),
            );
        }
    }
    found
}

/// `protection_unresolved(store, naked=)`: why a sleeve may not buy right now — its naked
/// positions, and every open issue recorded for it. Empty means clear.
#[must_use]
pub fn protection_unresolved(open_issues: &[PyDict], naked: &[PyDict]) -> Vec<String> {
    let mut reasons: Vec<String> = naked
        .iter()
        .map(|row| {
            format!(
                "{}: position {} holds {} with no resting stop",
                row.get("symbol").unwrap_or(&PyValue::None).py_str(),
                row.get("id").unwrap_or(&PyValue::None).py_str(),
                row.get("quantity_open").unwrap_or(&PyValue::None).py_str()
            )
        })
        .collect();
    for i in open_issues {
        reasons.push(format!(
            "{}: {} — {}",
            i.get("symbol").unwrap_or(&PyValue::None).py_str(),
            i.get("kind").unwrap_or(&PyValue::None).py_str(),
            i.get("detail").unwrap_or(&PyValue::None).py_str()
        ));
    }
    reasons
}

/// `refusal(reasons)`.
#[must_use]
pub fn refusal(reasons: &[String]) -> String {
    format!("PROTECTION_UNRESOLVED: {}", reasons.join("; "))
}

/// `PgIssueStore`: `lv_protection_issue` through the desk's sqlite-shaped connection; one open row
/// per `(user, sleeve, position, kind)`.
pub struct PgIssueStore<'c> {
    conn: &'c mut Conn,
    user_id: i64,
    schema: String,
}

impl<'c> PgIssueStore<'c> {
    /// A store for one user (`schema` `""` for an unqualified table, as on SQLite).
    pub fn new(conn: &'c mut Conn, user_id: i64, schema: &str) -> Self {
        Self {
            conn,
            user_id,
            schema: schema.to_owned(),
        }
    }

    fn t(&self, table: &str) -> String {
        if self.schema.is_empty() {
            table.to_owned()
        } else {
            format!("{}.{table}", self.schema)
        }
    }
}

fn err(e: &StorageError) -> HookError {
    format!("{e}")
}

fn db_i(v: Option<&DbValue>) -> i64 {
    v.and_then(DbValue::as_i64).unwrap_or(0)
}

fn db_s(v: Option<&DbValue>) -> String {
    v.and_then(DbValue::as_str).unwrap_or("").to_owned()
}

#[async_trait::async_trait]
impl IssueStore for PgIssueStore<'_> {
    async fn open_issues(&mut self, sleeve: Option<&str>) -> Result<Vec<OpenIssue>, HookError> {
        let mut sql = format!(
            "SELECT id, sleeve, position_id, symbol, kind, detail, seen_at FROM {} WHERE \
             user_id = ? AND resolved_at IS NULL",
            self.t("lv_protection_issue")
        );
        let mut params = vec![Param::Int(self.user_id)];
        if let Some(s) = sleeve {
            sql.push_str(" AND sleeve = ?");
            params.push(Param::Text(s.to_owned()));
        }
        sql.push_str(" ORDER BY seen_at, id");
        let cur = self
            .conn
            .execute(&sql, &params)
            .await
            .map_err(|e| err(&e))?;
        Ok(cur
            .rows
            .iter()
            .map(|r| OpenIssue {
                id: db_i(r.get("id")),
                sleeve: db_s(r.get("sleeve")),
                position_id: db_i(r.get("position_id")),
                symbol: db_s(r.get("symbol")),
                kind: db_s(r.get("kind")),
                detail: db_s(r.get("detail")),
                seen_at: r.get("seen_at").map_or(PyValue::None, super::db_to_py),
            })
            .collect())
    }

    async fn record(&mut self, issue: &Issue, now: DateTime<FixedOffset>) -> Result<(), HookError> {
        let sql = format!(
            "INSERT INTO {} (user_id, sleeve, position_id, symbol, kind, detail, seen_at) \
             VALUES (?, ?, ?, ?, ?, ?, ?) \
             ON CONFLICT (user_id, sleeve, position_id, kind) WHERE resolved_at IS NULL \
             DO UPDATE SET detail = EXCLUDED.detail, seen_at = EXCLUDED.seen_at",
            self.t("lv_protection_issue")
        );
        self.conn
            .execute(
                &sql,
                &[
                    Param::Int(self.user_id),
                    Param::Text(issue.sleeve.clone()),
                    Param::Int(issue.position_id),
                    Param::Text(issue.symbol.clone()),
                    Param::Text(issue.kind.clone()),
                    Param::Text(issue.detail.clone()),
                    Param::Aware(now),
                ],
            )
            .await
            .map_err(|e| err(&e))?;
        Ok(())
    }

    async fn resolve_except(
        &mut self,
        sleeve: &str,
        kinds: &BTreeSet<String>,
        keep: &HashSet<(i64, String)>,
        now: DateTime<FixedOffset>,
    ) -> Result<i64, HookError> {
        let mut resolved = 0;
        for issue in self.open_issues(Some(sleeve)).await? {
            if !kinds.contains(&issue.kind)
                || keep.contains(&(issue.position_id, issue.kind.clone()))
            {
                continue;
            }
            let sql = format!(
                "UPDATE {} SET resolved_at = ? WHERE id = ?",
                self.t("lv_protection_issue")
            );
            self.conn
                .execute(&sql, &[Param::Aware(now), Param::Int(issue.id)])
                .await
                .map_err(|e| err(&e))?;
            resolved += 1;
        }
        Ok(resolved)
    }
}

/// `MemoryIssueStore`: the same contract over a list — for tests and sleeve-store doubles.
#[derive(Debug, Clone, Default)]
pub struct MemoryIssueStore {
    /// Every row, resolved or not (`resolved_at` `None` = open).
    pub rows: Vec<(OpenIssue, Option<DateTime<FixedOffset>>)>,
    next: i64,
}

#[async_trait::async_trait]
impl IssueStore for MemoryIssueStore {
    async fn open_issues(&mut self, sleeve: Option<&str>) -> Result<Vec<OpenIssue>, HookError> {
        Ok(self
            .rows
            .iter()
            .filter(|(r, resolved)| resolved.is_none() && sleeve.is_none_or(|s| r.sleeve == s))
            .map(|(r, _)| r.clone())
            .collect())
    }

    async fn record(&mut self, issue: &Issue, now: DateTime<FixedOffset>) -> Result<(), HookError> {
        for (r, resolved) in &mut self.rows {
            if resolved.is_none()
                && r.sleeve == issue.sleeve
                && r.position_id == issue.position_id
                && r.kind == issue.kind
            {
                r.detail.clone_from(&issue.detail);
                r.seen_at = PyValue::Str(now.to_rfc3339());
                return Ok(());
            }
        }
        self.next += 1;
        self.rows.push((
            OpenIssue {
                id: self.next,
                sleeve: issue.sleeve.clone(),
                position_id: issue.position_id,
                symbol: issue.symbol.clone(),
                kind: issue.kind.clone(),
                detail: issue.detail.clone(),
                seen_at: PyValue::Str(now.to_rfc3339()),
            },
            None,
        ));
        Ok(())
    }

    async fn resolve_except(
        &mut self,
        sleeve: &str,
        kinds: &BTreeSet<String>,
        keep: &HashSet<(i64, String)>,
        now: DateTime<FixedOffset>,
    ) -> Result<i64, HookError> {
        let mut resolved = 0;
        for (r, at) in &mut self.rows {
            if at.is_some() || r.sleeve != sleeve || !kinds.contains(&r.kind) {
                continue;
            }
            if keep.contains(&(r.position_id, r.kind.clone())) {
                continue;
            }
            *at = Some(now);
            resolved += 1;
        }
        Ok(resolved)
    }
}

#[cfg(test)]
#[allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::float_cmp
)]
mod tests {
    //! `tests/test_reconcile.py`, ported where the reconciler itself is under test (the pass's
    //! counting, release, protection judgement, read-failure rule and the issue stores). The cases
    //! that drive TWT/VBT's own `on_order_update` handlers and stores belong to those lanes; here
    //! a sleeve double stands in for the handler (`FakeSleeve`).
    use super::*;
    use crate::wire::dict;
    use std::sync::Mutex;

    fn now() -> DateTime<FixedOffset> {
        DateTime::parse_from_rfc3339("2026-09-28T09:20:00+05:30").unwrap()
    }

    fn later() -> DateTime<FixedOffset> {
        now() + chrono::Duration::minutes(3)
    }

    #[derive(Default)]
    struct FakeBook {
        orders: Vec<PyValue>,
        gtts: Vec<PyValue>,
        holdings: Vec<PyDict>,
        fail: Vec<&'static str>,
    }

    impl FakeBook {
        fn report(&mut self, id: &str, status: &str, filled: i64) {
            self.orders
                .retain(|o| matches!(o, PyValue::Map(m) if pys(m.get("order_id")) != id));
            self.orders.push(PyValue::Map(dict([
                ("order_id", PyValue::Str(id.into())),
                ("status", PyValue::Str(status.into())),
                ("filled_quantity", PyValue::Int(filled)),
            ])));
        }

        fn gtt(&mut self, id: &str, symbol: &str, q: i64, status: &str) {
            self.gtts.push(PyValue::Map(dict([
                ("id", PyValue::Str(id.into())),
                ("status", PyValue::Str(status.into())),
                (
                    "condition",
                    PyValue::Map(dict([
                        ("tradingsymbol", PyValue::Str(symbol.into())),
                        ("trigger_values", PyValue::List(vec![PyValue::Float(80.0)])),
                    ])),
                ),
                (
                    "orders",
                    PyValue::List(vec![PyValue::Map(dict([
                        ("transaction_type", PyValue::Str("SELL".into())),
                        ("quantity", PyValue::Int(q)),
                    ]))]),
                ),
            ])));
        }
    }

    #[async_trait::async_trait]
    impl OrderBook for FakeBook {
        async fn orders(&self) -> Result<Vec<PyValue>, HookError> {
            if self.fail.contains(&"orders") {
                return Err("RuntimeError: order book unavailable".into());
            }
            Ok(self.orders.clone())
        }
        async fn get_gtts(&self) -> Result<Vec<PyValue>, HookError> {
            if self.fail.contains(&"gtts") {
                return Err("RuntimeError: gtt list unavailable".into());
            }
            Ok(self.gtts.clone())
        }
        async fn holdings(&self) -> Result<Vec<PyDict>, HookError> {
            if self.fail.contains(&"holdings") {
                return Err("RuntimeError: holdings unavailable".into());
            }
            Ok(self.holdings.clone())
        }
    }

    #[derive(Default)]
    struct FakeSleeve {
        orders: Vec<PyDict>,
        positions: Vec<PyDict>,
        risk: bool,
        released: Mutex<Vec<(String, f64)>>,
        updates: Mutex<Vec<String>>,
    }

    #[async_trait::async_trait]
    impl SleeveHooks for FakeSleeve {
        fn name(&self) -> &str {
            "twt"
        }
        async fn open_orders(&self) -> Result<Vec<PyDict>, HookError> {
            Ok(self.orders.clone())
        }
        async fn on_order_update(&self, payload: &PyDict) -> Result<Option<PyValue>, HookError> {
            self.updates
                .lock()
                .unwrap()
                .push(pys(payload.get("status")));
            Ok(Some(PyValue::Str("applied".into())))
        }
        async fn open_positions(&self) -> Result<Vec<PyDict>, HookError> {
            Ok(self.positions.clone())
        }
        fn has_risk(&self) -> bool {
            self.risk
        }
        fn release(&self, symbol: &str, value: f64) {
            self.released
                .lock()
                .unwrap()
                .push((symbol.to_owned(), value));
        }
    }

    fn sent(broker_id: &str, qty: i64) -> PyDict {
        dict([
            ("broker_order_id", PyValue::Str(broker_id.into())),
            ("symbol", PyValue::Str("TWTCO".into())),
            ("quantity", PyValue::Int(qty)),
            ("filled_quantity", PyValue::Int(0)),
            ("reference_price", PyValue::Str("100.00".into())),
        ])
    }

    fn position(id: i64, symbol: &str, gtt: Option<&str>, qty: i64, simulated: bool) -> PyDict {
        dict([
            ("id", PyValue::Int(id)),
            ("symbol", PyValue::Str(symbol.into())),
            ("quantity_open", PyValue::Int(qty)),
            (
                "gtt_id",
                gtt.map_or(PyValue::None, |g| PyValue::Str(g.into())),
            ),
            ("simulated", PyValue::Bool(simulated)),
        ])
    }

    #[tokio::test]
    async fn a_reported_order_is_handed_to_the_sleeves_handler() {
        let mut book = FakeBook::default();
        book.report("B1", "OPEN", 40);
        let sleeve = FakeSleeve {
            orders: vec![sent("B1", 100)],
            ..FakeSleeve::default()
        };
        let run = reconcile_once(&book, &[&sleeve], now(), &mut MemoryIssueStore::default()).await;
        assert_eq!((run.seen, run.applied), (1, 1));
        assert_eq!(*sleeve.updates.lock().unwrap(), ["OPEN"]);
    }

    #[tokio::test]
    async fn a_dead_order_releases_its_unfilled_remainder_to_the_risk_ledger() {
        let mut book = FakeBook::default();
        book.report("B1", "CANCELLED", 40);
        let sleeve = FakeSleeve {
            orders: vec![sent("B1", 100)],
            risk: true,
            ..FakeSleeve::default()
        };
        let run = reconcile_once(&book, &[&sleeve], now(), &mut MemoryIssueStore::default()).await;
        assert_eq!((run.dead, run.released), (1, 1));
        // 60 shares never filled, valued at the order's reference: 60 x 100 = 6,000.
        assert_eq!(
            *sleeve.released.lock().unwrap(),
            [("TWTCO".to_owned(), 6000.0)]
        );
    }

    #[tokio::test]
    async fn an_order_the_book_does_not_carry_is_counted_not_guessed() {
        let sleeve = FakeSleeve {
            orders: vec![sent("gone", 100)],
            ..FakeSleeve::default()
        };
        let run = reconcile_once(
            &FakeBook::default(),
            &[&sleeve],
            now(),
            &mut MemoryIssueStore::default(),
        )
        .await;
        assert_eq!((run.unseen, run.applied), (1, 0));
    }

    #[tokio::test]
    async fn naked_gtt_missing_oversized_and_external_exit_each_write_an_issue() {
        let mut book = FakeBook::default();
        book.gtt("778", "BIGCO", 150, "active");
        book.gtt("779", "GONECO", 100, "active");
        book.holdings = vec![dict([
            ("symbol", PyValue::Str("GONECO".into())),
            ("quantity", PyValue::Int(60)),
        ])];
        let sleeve = FakeSleeve {
            positions: vec![
                position(1, "NAKEDCO", None, 100, true),
                position(2, "MISSCO", Some("777"), 100, true),
                position(3, "BIGCO", Some("778"), 100, true),
                position(4, "GONECO", Some("779"), 100, false),
            ],
            ..FakeSleeve::default()
        };
        let mut issues = MemoryIssueStore::default();
        let run = reconcile_once(&book, &[&sleeve], now(), &mut issues).await;
        let kinds: HashSet<(i64, String)> = run
            .issues
            .iter()
            .map(|i| (i.position_id, i.kind.clone()))
            .collect();
        for (id, kind) in [
            (1, NAKED),
            (2, GTT_MISSING),
            (3, GTT_OVERSIZED),
            (4, EXTERNAL_EXIT),
        ] {
            assert!(kinds.contains(&(id, kind.to_owned())), "{id} {kind}");
        }
        assert_eq!(issues.open_issues(Some("twt")).await.unwrap().len(), 4);
    }

    #[tokio::test]
    async fn a_triggered_gtt_with_the_shares_still_held_is_an_unresolved_position() {
        let mut book = FakeBook::default();
        book.gtt("780", "FIRECO", 100, "triggered");
        book.holdings = vec![dict([
            ("symbol", PyValue::Str("FIRECO".into())),
            ("quantity", PyValue::Int(100)),
        ])];
        let sleeve = FakeSleeve {
            positions: vec![position(7, "FIRECO", Some("780"), 100, false)],
            ..FakeSleeve::default()
        };
        let run = reconcile_once(&book, &[&sleeve], now(), &mut MemoryIssueStore::default()).await;
        let kinds: Vec<(i64, String)> = run
            .issues
            .iter()
            .map(|i| (i.position_id, i.kind.clone()))
            .collect();
        assert_eq!(kinds, [(7, GTT_TRIGGERED_UNFILLED.to_owned())]);
        assert!(run.issues[0].detail.contains("the broker still shows 100"));
    }

    #[tokio::test]
    async fn an_issue_is_resolved_when_the_next_pass_no_longer_sees_it_and_kept_when_the_read_failed()
     {
        let mut book = FakeBook::default();
        let sleeve = FakeSleeve {
            positions: vec![position(2, "MISSCO", Some("777"), 1000, true)],
            ..FakeSleeve::default()
        };
        let mut issues = MemoryIssueStore::default();
        reconcile_once(&book, &[&sleeve], now(), &mut issues).await;
        let open: Vec<String> = issues
            .open_issues(Some("twt"))
            .await
            .unwrap()
            .into_iter()
            .map(|i| i.kind)
            .collect();
        assert_eq!(open, [GTT_MISSING]);
        book.fail.push("gtts");
        let failed = reconcile_once(&book, &[&sleeve], later(), &mut issues).await;
        assert!(!failed.errors.is_empty());
        assert_eq!(issues.open_issues(Some("twt")).await.unwrap().len(), 1);
        book.fail.clear();
        book.gtt("777", "MISSCO", 1000, "active");
        let run = reconcile_once(&book, &[&sleeve], later(), &mut issues).await;
        assert_eq!(run.resolved, 1);
        assert!(issues.open_issues(Some("twt")).await.unwrap().is_empty());
    }

    #[tokio::test]
    async fn an_unreadable_order_book_is_a_recorded_miss_not_a_crash() {
        let mut book = FakeBook::default();
        book.fail.push("orders");
        let sleeve = FakeSleeve {
            orders: vec![sent("B1", 100)],
            ..FakeSleeve::default()
        };
        let run = reconcile_once(&book, &[&sleeve], now(), &mut MemoryIssueStore::default()).await;
        assert!(!run.ok());
        assert!(
            run.line()
                .contains("errors: orders: RuntimeError: order book unavailable")
        );
        assert!(
            run.line()
                .starts_with("reconcile 09:20:00: 0 order(s) seen")
        );
    }

    #[test]
    fn the_guard_names_naked_positions_and_open_issues() {
        let naked = [dict([
            ("symbol", PyValue::Str("NAKEDCO".into())),
            ("id", PyValue::Int(1)),
            ("quantity_open", PyValue::Int(10)),
        ])];
        let open = [dict([
            ("symbol", PyValue::Str("MISSCO".into())),
            ("kind", PyValue::Str(GTT_MISSING.into())),
            (
                "detail",
                PyValue::Str("GTT 777 is not in the broker's list".into()),
            ),
        ])];
        let reasons = protection_unresolved(&open, &naked);
        assert_eq!(
            reasons[0],
            "NAKEDCO: position 1 holds 10 with no resting stop"
        );
        let r = refusal(&reasons);
        assert!(r.starts_with("PROTECTION_UNRESOLVED") && r.contains("GTT_MISSING"));
    }

    #[tokio::test]
    async fn pg_store_is_one_open_row_per_finding_and_resolve_except_keeps_what_was_seen() {
        let dir = tempfile::tempdir().unwrap();
        let mut conn = crate::storage::Db::connect_sqlite_path(&dir.path().join("i.db"))
            .await
            .unwrap();
        conn.execute(
            "CREATE TABLE lv_protection_issue (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, \
             sleeve TEXT, position_id INTEGER, symbol TEXT, kind TEXT, detail TEXT, seen_at TEXT, resolved_at TEXT)",
            &[],
        ).await.unwrap();
        conn.execute(
            "CREATE UNIQUE INDEX uq_open ON lv_protection_issue (user_id, sleeve, position_id, kind) WHERE resolved_at IS NULL",
            &[],
        ).await.unwrap();
        let mut store = PgIssueStore::new(&mut conn, 1, "");
        let issue = Issue {
            sleeve: "twt".into(),
            position_id: 9,
            symbol: "MISSCO".into(),
            kind: GTT_MISSING.into(),
            detail: "gone".into(),
        };
        store.record(&issue, now()).await.unwrap();
        let again = Issue {
            detail: "still gone".into(),
            ..issue.clone()
        };
        store.record(&again, later()).await.unwrap();
        let rows = store.open_issues(Some("twt")).await.unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].detail, "still gone");
        let all: BTreeSet<String> = ISSUE_KINDS.iter().map(|k| (*k).to_owned()).collect();
        let gtt: BTreeSet<String> = GTT_KINDS.iter().map(|k| (*k).to_owned()).collect();
        let keep: HashSet<(i64, String)> = [(9, GTT_MISSING.to_owned())].into_iter().collect();
        assert_eq!(
            store
                .resolve_except("twt", &all, &keep, later())
                .await
                .unwrap(),
            0
        );
        assert_eq!(
            store
                .resolve_except("twt", &gtt, &HashSet::new(), later())
                .await
                .unwrap(),
            1
        );
        assert!(store.open_issues(Some("twt")).await.unwrap().is_empty());
        store.record(&issue, later()).await.unwrap();
        assert_eq!(store.open_issues(None).await.unwrap().len(), 1);
    }

    #[test]
    fn gtt_index_reads_the_sell_leg_and_the_trigger() {
        let mut book = FakeBook::default();
        book.gtt("5", "ABC", 7, "ACTIVE");
        let idx = gtt_index(&book.gtts);
        let row = &idx["5"];
        assert_eq!(
            (row.status.as_str(), row.symbol.as_str(), row.quantity),
            ("active", "ABC", Some(7))
        );
        assert_eq!(row.trigger, Some(Decimal::new(800, 1)));
    }

    #[test]
    fn the_module_holds_no_writer() {
        let src = include_str!("reconcile.rs");
        let body = src.split("#[cfg(test)]").next().unwrap();
        for token in [
            concat!("Kite", "Writes"),
            "place_order",
            "place_gtt",
            "modify_gtt",
        ] {
            assert!(!body.contains(token), "{token}");
        }
    }
}
