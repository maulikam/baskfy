//! Durable order intents. A row is committed before the Kite POST, so a crash or a timeout can
//! never free a `client_id`; every desk process shares the primary key, so only one can send it.

use std::fmt::Write as _;
use std::sync::Arc;

use sha2::{Digest, Sha256};

use crate::clock::Clock;
use crate::storage::{Conn, Db, DbValue, Param, Row, StorageError};
use crate::wire::PyValue;

/// Committed; the POST may be in flight.
pub const SUBMITTING: &str = "SUBMITTING";
/// Kite holds it under `broker_order_id`.
pub const PLACED: &str = "PLACED";
/// Sent, or possibly sent, with no known outcome.
pub const UNKNOWN: &str = "UNKNOWN";
/// Definitively refused before the exchange.
pub const REJECTED: &str = "REJECTED";
/// The operator ruled it never reached Kite.
pub const NOT_SENT: &str = "NOT_SENT";
/// Simulated.
pub const DRY_RUN: &str = "DRY_RUN";

// Longer than the gateway's 7 s POST timeout, so an in-flight send is never judged by the book.
const RESOLVE_GRACE_SECONDS: f64 = 30.0;

const DDL: [&str; 3] = [
    "CREATE TABLE IF NOT EXISTS order_intent(
        client_id TEXT PRIMARY KEY,
        tag TEXT NOT NULL,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        qty INTEGER NOT NULL,
        product TEXT NOT NULL,
        order_type TEXT NOT NULL,
        state TEXT NOT NULL,
        broker_order_id TEXT,
        error TEXT,
        created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL
    )",
    "CREATE INDEX IF NOT EXISTS ix_order_intent_state ON order_intent(state)",
    "CREATE TABLE IF NOT EXISTS execution_control(
        id INTEGER PRIMARY KEY,
        paused INTEGER NOT NULL,
        reason TEXT,
        updated_at DOUBLE PRECISION NOT NULL
    )",
];

/// Kite's `tag` for a client id: 20 hex characters, the field's limit.
#[must_use]
pub fn kite_tag(client_id: &str) -> String {
    let digest = Sha256::digest(client_id.as_bytes());
    let mut tag = String::with_capacity(20);
    for byte in digest.iter().take(10) {
        let _ = write!(tag, "{byte:02x}");
    }
    tag
}

/// One order about to be sent.
#[derive(Debug, Clone, Copy)]
pub struct NewIntent<'a> {
    /// `plan_id:symbol`.
    pub client_id: &'a str,
    /// Kite's `tag`.
    pub tag: &'a str,
    /// Trading symbol.
    pub symbol: &'a str,
    /// `BUY` or `SELL`.
    pub side: &'a str,
    /// Quantity.
    pub qty: i64,
    /// `CNC`, `MTF`, ...
    pub product: &'a str,
    /// `MARKET`, `LIMIT`, ...
    pub order_type: &'a str,
}

/// What `reserve` decided.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Reservation {
    /// Committed; this caller may send.
    Proceed,
    /// Already at the broker (or simulated) under this order id.
    Placed(String),
    /// Sent, or possibly sent, with no known outcome: never send again until resolved.
    Unresolved(String),
    /// New submissions are paused.
    Paused(String),
}

/// The gateway's view of the store.
#[async_trait::async_trait]
pub trait IntentStore: Send + Sync {
    /// Commit the intent, or say why it must not be sent.
    ///
    /// # Errors
    /// The store cannot answer; the caller must not send.
    async fn reserve(&self, intent: &NewIntent<'_>) -> Result<Reservation, String>;

    /// Record the outcome of a send.
    ///
    /// # Errors
    /// The store cannot be written; the row stays open for the reconciler.
    async fn settle(
        &self,
        client_id: &str,
        state: &str,
        order_id: Option<&str>,
        error: Option<&str>,
    ) -> Result<(), String>;
}

/// An intent with no known outcome.
#[derive(Debug, Clone, PartialEq)]
pub struct OpenIntent {
    /// `plan_id:symbol`.
    pub client_id: String,
    /// Kite's `tag`.
    pub tag: String,
    /// Trading symbol.
    pub symbol: String,
    /// `SUBMITTING` or `UNKNOWN`.
    pub state: String,
    /// Last change, epoch seconds.
    pub updated_at: f64,
}

async fn prepared(db: &Db) -> Result<Conn, StorageError> {
    let mut conn = db.connect().await?;
    for statement in DDL {
        // Two processes racing the first CREATE can collide on Postgres's catalog; once is enough.
        if conn.execute(statement, &[]).await.is_err() {
            conn.execute(statement, &[]).await?;
        }
    }
    Ok(conn)
}

fn text(row: &Row, name: &str) -> String {
    row.get(name)
        .and_then(DbValue::as_str)
        .unwrap_or_default()
        .to_owned()
}

/// Whether new submissions are paused, and why.
///
/// # Errors
/// The database.
pub async fn pause_state(db: &Db) -> Result<(bool, String), StorageError> {
    let mut conn = prepared(db).await?;
    read_pause(&mut conn).await
}

async fn read_pause(conn: &mut Conn) -> Result<(bool, String), StorageError> {
    let cursor = conn
        .execute(
            "SELECT paused, reason FROM execution_control WHERE id = 1",
            &[],
        )
        .await?;
    Ok(cursor.fetchone().map_or((false, String::new()), |row| {
        (
            row.get("paused").and_then(DbValue::as_i64).unwrap_or(0) != 0,
            text(row, "reason"),
        )
    }))
}

/// Pause or resume new submissions. It survives a restart.
///
/// # Errors
/// The database.
pub async fn set_paused(db: &Db, now: f64, paused: bool, reason: &str) -> Result<(), StorageError> {
    let mut conn = prepared(db).await?;
    conn.execute(
        "INSERT INTO execution_control(id, paused, reason, updated_at) VALUES (1, ?, ?, ?) \
         ON CONFLICT(id) DO UPDATE SET paused = excluded.paused, reason = excluded.reason, \
         updated_at = excluded.updated_at",
        &[
            Param::Int(i64::from(paused)),
            Param::Text(reason.to_owned()),
            Param::Float(now),
        ],
    )
    .await?;
    Ok(())
}

/// Every intent with no known outcome, oldest first.
///
/// # Errors
/// The database.
pub async fn open_intents(db: &Db) -> Result<Vec<OpenIntent>, StorageError> {
    let mut conn = prepared(db).await?;
    read_open(&mut conn).await
}

async fn read_open(conn: &mut Conn) -> Result<Vec<OpenIntent>, StorageError> {
    let cursor = conn
        .execute(
            "SELECT client_id, tag, symbol, state, updated_at FROM order_intent \
             WHERE state IN ('SUBMITTING', 'UNKNOWN') ORDER BY updated_at",
            &[],
        )
        .await?;
    Ok(cursor
        .rows
        .iter()
        .map(|row| OpenIntent {
            client_id: text(row, "client_id"),
            tag: text(row, "tag"),
            symbol: text(row, "symbol"),
            state: text(row, "state"),
            updated_at: row
                .get("updated_at")
                .and_then(DbValue::as_f64)
                .unwrap_or(0.0),
        })
        .collect())
}

fn broker_order_for(orders: &[PyValue], tag: &str) -> Option<String> {
    orders.iter().find_map(|order| {
        let PyValue::Map(map) = order else {
            return None;
        };
        match (map.get("tag"), map.get("order_id")) {
            (Some(PyValue::Str(found)), Some(PyValue::Str(id))) if found == tag => Some(id.clone()),
            _ => None,
        }
    })
}

/// Match open intents to today's order book by tag. A match is `PLACED`; no match proves nothing
/// (the book is day-scoped and may lag), so the intent stays open for the operator.
///
/// # Errors
/// The database.
pub async fn resolve_from_order_book(
    db: &Db,
    now: f64,
    orders: &[PyValue],
) -> Result<(usize, usize), StorageError> {
    let mut conn = prepared(db).await?;
    let (mut resolved, mut still_open) = (0, 0);
    for intent in read_open(&mut conn).await? {
        if now - intent.updated_at < RESOLVE_GRACE_SECONDS {
            continue;
        }
        let Some(order_id) = broker_order_for(orders, &intent.tag) else {
            conn.execute(
                "UPDATE order_intent SET state = 'UNKNOWN' \
                 WHERE client_id = ? AND state = 'SUBMITTING'",
                &[Param::Text(intent.client_id.clone())],
            )
            .await?;
            still_open += 1;
            continue;
        };
        settle_on(
            &mut conn,
            now,
            &intent.client_id,
            PLACED,
            Some(&order_id),
            None,
        )
        .await?;
        resolved += 1;
    }
    Ok((resolved, still_open))
}

/// The operator's ruling on an open intent. `false` when the client id has none open.
///
/// # Errors
/// The database.
pub async fn resolve_by_operator(
    db: &Db,
    now: f64,
    client_id: &str,
    state: &str,
    order_id: Option<&str>,
) -> Result<bool, StorageError> {
    let mut conn = prepared(db).await?;
    settle_on(&mut conn, now, client_id, state, order_id, None).await
}

async fn settle_on(
    conn: &mut Conn,
    now: f64,
    client_id: &str,
    state: &str,
    order_id: Option<&str>,
    error: Option<&str>,
) -> Result<bool, StorageError> {
    let cursor = conn
        .execute(
            "UPDATE order_intent SET state = ?, broker_order_id = ?, error = ?, updated_at = ? \
             WHERE client_id = ? AND state IN ('SUBMITTING', 'UNKNOWN') RETURNING client_id",
            &[
                Param::Text(state.to_owned()),
                order_id.map(str::to_owned).into(),
                error.map(str::to_owned).into(),
                Param::Float(now),
                Param::Text(client_id.to_owned()),
            ],
        )
        .await?;
    Ok(cursor.fetchone().is_some())
}

/// [`IntentStore`] over the desk's database.
pub struct DbIntentStore {
    db: Db,
    clock: Arc<dyn Clock>,
}

impl DbIntentStore {
    /// Over the desk's database.
    #[must_use]
    pub fn new(db: Db, clock: Arc<dyn Clock>) -> Self {
        Self { db, clock }
    }

    async fn try_reserve(&self, i: &NewIntent<'_>) -> Result<Reservation, StorageError> {
        let mut conn = prepared(&self.db).await?;
        let (paused, reason) = read_pause(&mut conn).await?;
        if paused {
            return Ok(Reservation::Paused(reason));
        }
        let now = self.clock.time();
        // A definitive refusal frees the id for a corrected retry; nothing else ever does.
        let cursor = conn
            .execute(
                "INSERT INTO order_intent(client_id, tag, symbol, side, qty, product, order_type, \
                 state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'SUBMITTING', ?, ?) \
                 ON CONFLICT(client_id) DO UPDATE SET state = 'SUBMITTING', error = NULL, \
                 updated_at = excluded.updated_at \
                 WHERE order_intent.state IN ('REJECTED', 'NOT_SENT') RETURNING client_id",
                &[
                    Param::Text(i.client_id.to_owned()),
                    Param::Text(i.tag.to_owned()),
                    Param::Text(i.symbol.to_owned()),
                    Param::Text(i.side.to_owned()),
                    Param::Int(i.qty),
                    Param::Text(i.product.to_owned()),
                    Param::Text(i.order_type.to_owned()),
                    Param::Float(now),
                    Param::Float(now),
                ],
            )
            .await?;
        if cursor.fetchone().is_some() {
            return Ok(Reservation::Proceed);
        }
        let existing = conn
            .execute(
                "SELECT state, broker_order_id FROM order_intent WHERE client_id = ?",
                &[Param::Text(i.client_id.to_owned())],
            )
            .await?;
        let Some(row) = existing.fetchone() else {
            return Err(StorageError::Other(format!(
                "{}: the intent neither inserted nor exists",
                i.client_id
            )));
        };
        let state = text(row, "state");
        Ok(match state.as_str() {
            PLACED | DRY_RUN => Reservation::Placed(text(row, "broker_order_id")),
            _ => Reservation::Unresolved(state),
        })
    }
}

#[async_trait::async_trait]
impl IntentStore for DbIntentStore {
    async fn reserve(&self, intent: &NewIntent<'_>) -> Result<Reservation, String> {
        self.try_reserve(intent).await.map_err(|e| e.to_string())
    }

    async fn settle(
        &self,
        client_id: &str,
        state: &str,
        order_id: Option<&str>,
        error: Option<&str>,
    ) -> Result<(), String> {
        let mut conn = prepared(&self.db).await.map_err(|e| e.to_string())?;
        settle_on(
            &mut conn,
            self.clock.time(),
            client_id,
            state,
            order_id,
            error,
        )
        .await
        .map(|_| ())
        .map_err(|e| e.to_string())
    }
}
