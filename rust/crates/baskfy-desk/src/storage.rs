//! Ports `app.analytics.db` (connection, `transaction`, `schema_version`, `migrate`) and
//! `app.analytics.pg` (the Postgres backend wearing sqlite3's interface) — DECISIONS-RS R8.2.
//!
//! **What the desk really uses.** In this deployment the desk runs `DESK_DB_BACKEND=postgres`
//! (`compose.prod.yml`, `Dockerfile.desk`): its own tables in schema `desk` of the `baskfy`
//! database (`search_path=desk`), and the sleeves' tables (`sw_*`, `tw_*`, `vb_*`, `fo_*`,
//! `op_*`, `risk_ledger`) in `public`, qualified by name. A laptop desk and the Python tests run
//! SQLite (`DB_PATH`, `data/portfolio.db`), which stays the code default. Both are mirrored:
//!
//! * [`Db`] picks the backend exactly as `db.connect(path=None)` does: Postgres only when
//!   `DESK_DB_BACKEND == "postgres"` and no explicit path is given; an explicit path always
//!   means SQLite.
//! * Every statement is written in the **SQLite dialect with `?` placeholders**, as the Python
//!   is; on Postgres it goes through [`translate`] (a byte-for-byte port of `pg.translate`:
//!   the `AUTOINCREMENT` rewrite, `strftime` → `to_char`, the `INSERT OR …` refusal, `?` → `%s`
//!   outside literals and `%` → `%%` everywhere).
//! * **Parameters reach Postgres as psycopg sends them** (R8.2): a `str` is *untyped* (OID 0,
//!   the server infers it — the swing store relies on that for ISO dates and decimals), which
//!   sqlx's binary binds cannot express. So a Postgres statement runs on the simple-query
//!   protocol with every parameter rendered as a quoted literal ([`pg_literal`]): a quoted
//!   string literal *is* an untyped constant, the same inference psycopg's unknown-OID bind
//!   gets. Quoting is standard-conforming (`'` doubled, NUL refused, `standard_conforming_strings`
//!   checked on): no injection path. And psycopg's own quirk is kept: with **no** parameters it
//!   does no `%` processing, so a translated `LIKE '%%x%%'` reaches the server doubled.
//! * **Rows come back as the adapter returns them** ([`DbValue`]): `numeric` → float, `date` →
//!   ISO string (`pg._native`), everything else by its type; SQLite's storage classes as
//!   `sqlite3.Row` gives them. [`Row`] is indexable by name and position, like `sqlite3.Row`.
//! * Connections are autocommit; [`Conn::transaction`] is `db.transaction` (`BEGIN IMMEDIATE`
//!   on SQLite, `BEGIN` on Postgres; rollback on error).
//!
//! `lastrowid` on Postgres refuses (Python raises `NotImplementedError`): callers use
//! `RETURNING`.

mod migrations;

use std::fmt::Write as _;
use std::path::PathBuf;
use std::str::FromStr;

use chrono::{DateTime, FixedOffset, NaiveDate, NaiveDateTime};
use futures_util::TryStreamExt;
use rust_decimal::Decimal;
use sqlx::pool::PoolConnection;
use sqlx::postgres::{PgConnectOptions, PgPoolOptions};
use sqlx::sqlite::{SqliteConnectOptions, SqliteJournalMode, SqlitePoolOptions, SqliteSynchronous};
use sqlx::{Column, Either, Postgres, Row as _, Sqlite, TypeInfo, ValueRef};

pub use migrations::MIGRATIONS;

/// What can go wrong talking to the desk's database.
#[derive(Debug, thiserror::Error)]
pub enum StorageError {
    /// `pg.translate` refused a statement (`INSERT OR IGNORE`, an unknown `strftime` format).
    #[error("ValueError: {0}")]
    Untranslatable(String),
    /// A parameter Python's driver would also refuse (NUL in text, `Decimal` on sqlite3).
    #[error("{0}")]
    BadParameter(String),
    /// `cursor.lastrowid` on Postgres (`NotImplementedError` in Python).
    #[error("lastrowid has no Postgres equivalent — use `INSERT ... RETURNING id`")]
    NoLastRowId,
    /// The server or driver.
    #[error("{0}")]
    Driver(#[from] sqlx::Error),
    /// Anything else (a value the decoder does not know, a refused connection setting).
    #[error("{0}")]
    Other(String),
}

/// A bound parameter, by the Python type the call site passes.
#[derive(Debug, Clone, PartialEq)]
pub enum Param {
    /// `None`.
    Null,
    /// `bool`.
    Bool(bool),
    /// `int`.
    Int(i64),
    /// `float`.
    Float(f64),
    /// `str`.
    Text(String),
    /// `decimal.Decimal` (psycopg sends `numeric`; sqlite3 refuses it).
    Decimal(Decimal),
    /// `datetime.date`.
    Date(NaiveDate),
    /// A naive `datetime.datetime`.
    Naive(NaiveDateTime),
    /// An aware `datetime.datetime`.
    Aware(DateTime<FixedOffset>),
    /// `psycopg.types.json.Jsonb(...)`: the JSON text.
    Jsonb(String),
    /// `bytes`.
    Bytes(Vec<u8>),
}

impl From<&str> for Param {
    fn from(v: &str) -> Self {
        Self::Text(v.to_owned())
    }
}
impl From<String> for Param {
    fn from(v: String) -> Self {
        Self::Text(v)
    }
}
impl From<i64> for Param {
    fn from(v: i64) -> Self {
        Self::Int(v)
    }
}
impl From<f64> for Param {
    fn from(v: f64) -> Self {
        Self::Float(v)
    }
}
impl From<bool> for Param {
    fn from(v: bool) -> Self {
        Self::Bool(v)
    }
}
impl<T: Into<Self>> From<Option<T>> for Param {
    fn from(v: Option<T>) -> Self {
        v.map_or(Self::Null, Into::into)
    }
}

/// A column value as the Python desk's connection hands it back.
#[derive(Debug, Clone, PartialEq)]
pub enum DbValue {
    /// `None`.
    Null,
    /// `int` (SQLite INTEGER; Postgres int2/4/8, oid).
    Int(i64),
    /// `float` (SQLite REAL; Postgres float4/8 and **numeric**, as `pg._native` converts).
    Float(f64),
    /// `str` (SQLite TEXT; Postgres text/varchar/…; a Postgres **date** as its ISO string).
    Text(String),
    /// `bytes`.
    Bytes(Vec<u8>),
    /// `bool` (Postgres only; SQLite has none).
    Bool(bool),
    /// An aware `datetime` (Postgres `timestamptz`).
    Aware(DateTime<FixedOffset>),
    /// A naive `datetime` (Postgres `timestamp`).
    Naive(NaiveDateTime),
    /// A parsed `json`/`jsonb` value, as its JSON text.
    Json(String),
}

impl DbValue {
    /// `value is None`.
    #[must_use]
    pub const fn is_null(&self) -> bool {
        matches!(self, Self::Null)
    }

    /// The `str`, if it is one.
    #[must_use]
    pub fn as_str(&self) -> Option<&str> {
        match self {
            Self::Text(s) | Self::Json(s) => Some(s),
            _ => None,
        }
    }

    /// `int(value)` for an int (or a bool), `None` otherwise.
    #[must_use]
    pub const fn as_i64(&self) -> Option<i64> {
        match self {
            Self::Int(i) => Some(*i),
            Self::Bool(b) => Some(*b as i64),
            _ => None,
        }
    }

    /// `float(value)` for a number, `None` otherwise.
    #[must_use]
    #[allow(clippy::cast_precision_loss)]
    pub const fn as_f64(&self) -> Option<f64> {
        match self {
            Self::Float(f) => Some(*f),
            Self::Int(i) => Some(*i as f64),
            _ => None,
        }
    }
}

/// `sqlite3.Row` / `pg.Row`: ordered columns, by name or by position.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Row(pub Vec<(String, DbValue)>);

impl Row {
    /// `row["name"]`.
    #[must_use]
    pub fn get(&self, name: &str) -> Option<&DbValue> {
        self.0.iter().find(|(k, _)| k == name).map(|(_, v)| v)
    }

    /// `row[i]`.
    #[must_use]
    pub fn at(&self, index: usize) -> Option<&DbValue> {
        self.0.get(index).map(|(_, v)| v)
    }

    /// `row.keys()`.
    #[must_use]
    pub fn keys(&self) -> Vec<&str> {
        self.0.iter().map(|(k, _)| k.as_str()).collect()
    }
}

/// What `conn.execute(...)` returned: the cursor's rows, `rowcount`, and (SQLite) `lastrowid`.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Cursor {
    /// `fetchall()`.
    pub rows: Vec<Row>,
    /// `cursor.rowcount` (rows affected; for a SELECT, SQLite reports -1, psycopg the count).
    pub rowcount: i64,
    last_insert_id: Option<i64>,
    postgres: bool,
}

impl Cursor {
    /// `fetchone()`.
    #[must_use]
    pub fn fetchone(&self) -> Option<&Row> {
        self.rows.first()
    }

    /// `cursor.lastrowid`.
    ///
    /// # Errors
    /// [`StorageError::NoLastRowId`] on Postgres, as `pg.Cursor.lastrowid` raises.
    pub fn lastrowid(&self) -> Result<Option<i64>, StorageError> {
        if self.postgres {
            return Err(StorageError::NoLastRowId);
        }
        Ok(self.last_insert_id)
    }
}

// ---------------------------------------------------------------------------------------------
// pg.translate — byte-for-byte
// ---------------------------------------------------------------------------------------------

fn case_insensitive_replace(text: &str, old: &str, new: &str) -> String {
    let lower = text.to_ascii_lowercase();
    let needle = old.to_ascii_lowercase();
    let mut out = String::with_capacity(text.len());
    let mut last = 0;
    let mut from = 0;
    while let Some(pos) = lower.get(from..).and_then(|s| s.find(&needle)) {
        let at = from + pos;
        out.push_str(&text[last..at]);
        out.push_str(new);
        last = at + old.len();
        from = last;
    }
    out.push_str(&text[last..]);
    out
}

/// `_STRFTIME`'s replacement: `strftime('<fmt>', <col>)` → `to_char((<col>)::date, '<FMT>')`.
fn translate_strftime(sql: &str) -> Result<String, StorageError> {
    // `strftime\(\s*'([^']+)'\s*,\s*([^)]+)\)`, case-insensitive.
    let re = regex::RegexBuilder::new(r"strftime\(\s*'([^']+)'\s*,\s*([^)]+)\)")
        .case_insensitive(true)
        .build()
        .map_err(|e| StorageError::Other(e.to_string()))?;
    let mut out = String::with_capacity(sql.len());
    let mut last = 0;
    for caps in re.captures_iter(sql) {
        let (Some(whole), Some(pattern), Some(column)) = (caps.get(0), caps.get(1), caps.get(2))
        else {
            continue;
        };
        let to = match pattern.as_str() {
            "%Y-%m-%d" => "YYYY-MM-DD",
            "%Y-%m" => "YYYY-MM",
            "%Y" => "YYYY",
            "%W" => "IW",
            "%w" => "ID",
            other => {
                return Err(StorageError::Untranslatable(format!(
                    "no Postgres translation for strftime pattern {}",
                    baskfy_providers::py::repr_str(other)
                )));
            }
        };
        out.push_str(&sql[last..whole.start()]);
        let _ = write!(out, "to_char(({})::date, '{to}')", column.as_str().trim());
        last = whole.end();
    }
    out.push_str(&sql[last..]);
    Ok(out)
}

/// Ports `app.analytics.pg.translate`: SQLite SQL to Postgres SQL, for the constructs the desk
/// uses.
///
/// # Errors
/// `INSERT OR IGNORE|REPLACE|…` and an unknown `strftime` pattern, as Python raises.
pub fn translate(sql: &str) -> Result<String, StorageError> {
    let refused =
        regex::RegexBuilder::new(r"\bINSERT\s+OR\s+(IGNORE|REPLACE|ABORT|FAIL|ROLLBACK)\b")
            .case_insensitive(true)
            .build()
            .map_err(|e| StorageError::Other(e.to_string()))?;
    if let Some(m) = refused.find(sql) {
        return Err(StorageError::Untranslatable(format!(
            "{} has no Postgres equivalent and must not be dropped: write the conflict clause the \
             statement means, e.g. 'INSERT INTO t(...) VALUES(...) ON CONFLICT(key) DO NOTHING'. \
             Both SQLite and Postgres accept that spelling.",
            baskfy_providers::py::repr_str(m.as_str())
        )));
    }
    let mut out = case_insensitive_replace(
        sql,
        "INTEGER PRIMARY KEY AUTOINCREMENT",
        "BIGSERIAL PRIMARY KEY",
    );
    out = case_insensitive_replace(&out, "AUTOINCREMENT", "");
    out = translate_strftime(&out)?;
    Ok(placeholders(&out))
}

/// Ports `app.analytics.pg.placeholders`: `?` → `%s` outside string literals, `%` → `%%`
/// everywhere, `''` inside a literal kept as an escaped quote.
#[must_use]
pub fn placeholders(sql: &str) -> String {
    let chars: Vec<char> = sql.chars().collect();
    let mut out = String::with_capacity(sql.len() + 8);
    let mut in_string = false;
    let mut i = 0;
    while let Some(&c) = chars.get(i) {
        if c == '\'' {
            if in_string && chars.get(i + 1) == Some(&'\'') {
                out.push_str("''");
                i += 2;
                continue;
            }
            in_string = !in_string;
            out.push(c);
        } else if c == '%' {
            out.push_str("%%");
        } else if c == '?' && !in_string {
            out.push_str("%s");
        } else {
            out.push(c);
        }
        i += 1;
    }
    out
}

/// One parameter as a Postgres literal with psycopg's typing (R8.2): `str` untyped, the rest
/// cast to the type psycopg would send.
///
/// # Errors
/// A NUL byte in text (psycopg: "PostgreSQL text fields cannot contain NUL (0x00) bytes").
pub fn pg_literal(param: &Param) -> Result<String, StorageError> {
    fn quote(s: &str) -> Result<String, StorageError> {
        if s.contains('\0') {
            return Err(StorageError::BadParameter(
                "PostgreSQL text fields cannot contain NUL (0x00) bytes".to_owned(),
            ));
        }
        Ok(format!("'{}'", s.replace('\'', "''")))
    }
    Ok(match param {
        Param::Null => "NULL".to_owned(),
        Param::Bool(b) => if *b { "true" } else { "false" }.to_owned(),
        Param::Int(i) => i.to_string(),
        Param::Float(f) => {
            let text = if f.is_nan() {
                "NaN".to_owned()
            } else if f.is_infinite() {
                if *f > 0.0 { "Infinity" } else { "-Infinity" }.to_owned()
            } else {
                baskfy_core::py::float_repr(*f)
            };
            format!("'{text}'::float8")
        }
        Param::Text(s) => quote(s)?,
        Param::Decimal(d) => format!("'{d}'::numeric"),
        Param::Date(d) => format!("'{}'::date", d.format("%Y-%m-%d")),
        Param::Naive(t) => format!("'{}'::timestamp", t.format("%Y-%m-%d %H:%M:%S%.f")),
        Param::Aware(t) => format!("'{}'::timestamptz", t.format("%Y-%m-%d %H:%M:%S%.f%:z")),
        Param::Jsonb(j) => format!("{}::jsonb", quote(j)?),
        Param::Bytes(b) => {
            let mut hex = String::with_capacity(b.len() * 2 + 4);
            hex.push_str("'\\x");
            for byte in b {
                let _ = write!(hex, "{byte:02x}");
            }
            hex.push_str("'::bytea");
            hex
        }
    })
}

/// psycopg's client-side view of a translated statement: with parameters, `%s` takes the next
/// literal and `%%` becomes `%`; with none, the text goes as it is (doubled `%%` and all).
///
/// # Errors
/// A bad parameter, or a placeholder/parameter count mismatch (psycopg's `ProgrammingError`).
pub fn pg_render(translated: &str, params: &[Param]) -> Result<String, StorageError> {
    if params.is_empty() {
        return Ok(translated.to_owned());
    }
    let mut out = String::with_capacity(translated.len() + params.len() * 8);
    let mut next = params.iter();
    let mut chars = translated.chars().peekable();
    while let Some(c) = chars.next() {
        if c != '%' {
            out.push(c);
            continue;
        }
        match chars.next() {
            Some('%') => out.push('%'),
            Some('s') => {
                let Some(p) = next.next() else {
                    return Err(StorageError::BadParameter(format!(
                        "the query has more placeholders than the {} parameters passed",
                        params.len()
                    )));
                };
                out.push_str(&pg_literal(p)?);
            }
            other => {
                return Err(StorageError::BadParameter(format!(
                    "only '%s', '%b', '%t' are allowed as placeholders, got '%{}'",
                    other.map(String::from).unwrap_or_default()
                )));
            }
        }
    }
    if next.next().is_some() {
        return Err(StorageError::BadParameter(format!(
            "the query has fewer placeholders than the {} parameters passed",
            params.len()
        )));
    }
    Ok(out)
}

// ---------------------------------------------------------------------------------------------
// Connections
// ---------------------------------------------------------------------------------------------

/// Which backend, and where — `app.analytics.db`'s module state.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DbConfig {
    /// `DESK_DB_BACKEND` (`"sqlite"` default, `"postgres"`).
    pub backend: String,
    /// `DESK_DB_SCHEMA` (`"desk"`).
    pub schema: String,
    /// `DESK_DATABASE_URL`, read at connect time in Python (`_pg_dsn`).
    pub database_url: String,
    /// `config.DB_PATH`.
    pub db_path: PathBuf,
}

impl DbConfig {
    /// From the settings and the function-level `DESK_DATABASE_URL`.
    #[must_use]
    pub fn from_settings(
        settings: &crate::settings::Settings,
        database_url: Option<String>,
    ) -> Self {
        Self {
            backend: settings.db_backend.clone(),
            schema: settings.db_schema.clone(),
            database_url: database_url
                .unwrap_or_else(|| "postgresql://baskfy:baskfy@localhost:5433/baskfy".to_owned()),
            db_path: PathBuf::from(&settings.db_path),
        }
    }

    /// `db.DB_BACKEND == "postgres"`.
    #[must_use]
    pub fn is_postgres(&self) -> bool {
        self.backend == "postgres"
    }
}

/// The desk's database handle: a small pool per backend, built lazily.
#[derive(Debug, Clone)]
pub struct Db {
    config: DbConfig,
    pg: Option<sqlx::PgPool>,
    sqlite: Option<sqlx::SqlitePool>,
}

fn sqlite_options(path: &std::path::Path) -> SqliteConnectOptions {
    SqliteConnectOptions::new()
        .filename(path)
        .create_if_missing(true)
        .journal_mode(SqliteJournalMode::Wal)
        .synchronous(SqliteSynchronous::Normal)
        .foreign_keys(true)
        .busy_timeout(std::time::Duration::from_secs(30))
}

impl Db {
    /// Open the configured backend (`db.connect()` with no path).
    ///
    /// # Errors
    /// A bad URL, or `standard_conforming_strings` off on the server (R8.2's quoting needs it).
    pub async fn open(config: DbConfig) -> Result<Self, StorageError> {
        if config.is_postgres() {
            let options = PgConnectOptions::from_str(&config.database_url)?
                .options([("search_path", config.schema.as_str())])
                .application_name("baskfy-desk");
            let pool = PgPoolOptions::new()
                .max_connections(8)
                .connect_lazy_with(options);
            let db = Self {
                config,
                pg: Some(pool),
                sqlite: None,
            };
            Ok(db)
        } else {
            if let Some(parent) = config.db_path.parent()
                && !parent.as_os_str().is_empty()
            {
                std::fs::create_dir_all(parent).map_err(|e| StorageError::Other(e.to_string()))?;
            }
            let pool = SqlitePoolOptions::new()
                .max_connections(4)
                .connect_lazy_with(sqlite_options(&config.db_path));
            Ok(Self {
                config,
                pg: None,
                sqlite: Some(pool),
            })
        }
    }

    /// The configuration it was opened with.
    #[must_use]
    pub const fn config(&self) -> &DbConfig {
        &self.config
    }

    /// `db.connect()`: one connection for the duration of a `with` block.
    ///
    /// # Errors
    /// The pool's.
    pub async fn connect(&self) -> Result<Conn, StorageError> {
        if let Some(pool) = &self.pg {
            let mut conn = pool.acquire().await?;
            let row: (String,) = sqlx::query_as("SHOW standard_conforming_strings")
                .fetch_one(&mut *conn)
                .await?;
            if row.0 != "on" {
                return Err(StorageError::Other(
                    "standard_conforming_strings is off; the desk's literal quoting needs it on"
                        .to_owned(),
                ));
            }
            return Ok(Conn::Postgres(conn));
        }
        match &self.sqlite {
            Some(pool) => Ok(Conn::Sqlite(pool.acquire().await?)),
            None => Err(StorageError::Other("no backend configured".to_owned())),
        }
    }

    /// `db.connect(path)`: an explicit path always means SQLite (tests pass `tmp_path`).
    ///
    /// # Errors
    /// The driver's.
    pub async fn connect_sqlite_path(path: &std::path::Path) -> Result<Conn, StorageError> {
        use sqlx::Connection as _;
        let conn = sqlx::SqliteConnection::connect_with(&sqlite_options(path)).await?;
        Ok(Conn::SqliteOwned(Box::new(conn)))
    }
}

/// One connection: `sqlite3.Connection` or `pg.Connection`.
#[derive(Debug)]
pub enum Conn {
    /// From the SQLite pool.
    Sqlite(PoolConnection<Sqlite>),
    /// An explicit-path SQLite connection.
    SqliteOwned(Box<sqlx::SqliteConnection>),
    /// From the Postgres pool (autocommit, `search_path` set).
    Postgres(PoolConnection<Postgres>),
}

fn sqlite_value(value: &sqlx::sqlite::SqliteValueRef<'_>) -> Result<DbValue, StorageError> {
    if value.is_null() {
        return Ok(DbValue::Null);
    }
    let kind = value.type_info().name().to_owned();
    let owned = sqlx::ValueRef::to_owned(value);
    let r = sqlx::Value::as_ref(&owned);
    let decoded = match kind.as_str() {
        "INTEGER" | "BOOLEAN" => {
            DbValue::Int(<i64 as sqlx::Decode<Sqlite>>::decode(r).map_err(sqlx::Error::Decode)?)
        }
        "REAL" => {
            DbValue::Float(<f64 as sqlx::Decode<Sqlite>>::decode(r).map_err(sqlx::Error::Decode)?)
        }
        "BLOB" => DbValue::Bytes(
            <Vec<u8> as sqlx::Decode<Sqlite>>::decode(r).map_err(sqlx::Error::Decode)?,
        ),
        _ => {
            DbValue::Text(<String as sqlx::Decode<Sqlite>>::decode(r).map_err(sqlx::Error::Decode)?)
        }
    };
    Ok(decoded)
}

fn sqlite_rows(rows: &[sqlx::sqlite::SqliteRow]) -> Result<Vec<Row>, StorageError> {
    rows.iter()
        .map(|row| {
            row.columns()
                .iter()
                .map(|col| {
                    let raw = row.try_get_raw(col.ordinal())?;
                    Ok((col.name().to_owned(), sqlite_value(&raw)?))
                })
                .collect::<Result<Vec<_>, StorageError>>()
                .map(Row)
        })
        .collect()
}

fn pg_text_value(type_name: &str, text: &str) -> Result<DbValue, StorageError> {
    let bad = |what: &str| StorageError::Other(format!("cannot read {what} {text:?}"));
    Ok(match type_name {
        "INT2" | "INT4" | "INT8" | "OID" => DbValue::Int(text.parse().map_err(|_| bad(type_name))?),
        "FLOAT4" | "FLOAT8" | "NUMERIC" => DbValue::Float(match text {
            "NaN" => f64::NAN,
            "Infinity" => f64::INFINITY,
            "-Infinity" => f64::NEG_INFINITY,
            _ => text.parse().map_err(|_| bad(type_name))?,
        }),
        "BOOL" => DbValue::Bool(text == "t"),
        "TIMESTAMPTZ" => DbValue::Aware(
            DateTime::parse_from_str(&format!("{text}00"), "%Y-%m-%d %H:%M:%S%.f%z")
                .or_else(|_| DateTime::parse_from_str(text, "%Y-%m-%d %H:%M:%S%.f%#z"))
                .map_err(|_| bad(type_name))?,
        ),
        "TIMESTAMP" => DbValue::Naive(
            NaiveDateTime::parse_from_str(text, "%Y-%m-%d %H:%M:%S%.f")
                .map_err(|_| bad(type_name))?,
        ),
        "JSON" | "JSONB" => DbValue::Json(text.to_owned()),
        // DATE stays the ISO string `pg._native` makes of it; text-like types are text.
        _ => DbValue::Text(text.to_owned()),
    })
}

fn pg_rows(rows: &[sqlx::postgres::PgRow]) -> Result<Vec<Row>, StorageError> {
    rows.iter()
        .map(|row| {
            row.columns()
                .iter()
                .map(|col| {
                    let raw = row.try_get_raw(col.ordinal())?;
                    let value = if raw.is_null() {
                        DbValue::Null
                    } else {
                        let text = raw.as_str().map_err(|e| {
                            StorageError::Other(format!("column {}: {e}", col.name()))
                        })?;
                        pg_text_value(col.type_info().name(), text)?
                    };
                    Ok((col.name().to_owned(), value))
                })
                .collect::<Result<Vec<_>, StorageError>>()
                .map(Row)
        })
        .collect()
}

fn sqlite_bind<'q>(
    mut query: sqlx::query::Query<'q, Sqlite, sqlx::sqlite::SqliteArguments<'q>>,
    params: &'q [Param],
) -> Result<sqlx::query::Query<'q, Sqlite, sqlx::sqlite::SqliteArguments<'q>>, StorageError> {
    for (i, p) in params.iter().enumerate() {
        query = match p {
            Param::Null => query.bind(None::<i64>),
            Param::Bool(b) => query.bind(i64::from(*b)),
            Param::Int(v) => query.bind(*v),
            Param::Float(v) => query.bind(*v),
            Param::Text(s) | Param::Jsonb(s) => query.bind(s.as_str()),
            Param::Bytes(b) => query.bind(b.as_slice()),
            // sqlite3's default (deprecated) adapters: date.isoformat(), datetime.isoformat(" ").
            Param::Date(d) => query.bind(d.format("%Y-%m-%d").to_string()),
            Param::Naive(t) => query.bind(t.format("%Y-%m-%d %H:%M:%S%.f").to_string()),
            Param::Aware(t) => query.bind(t.format("%Y-%m-%d %H:%M:%S%.f%:z").to_string()),
            Param::Decimal(_) => {
                return Err(StorageError::BadParameter(format!(
                    "Error binding parameter {}: type 'decimal.Decimal' is not supported",
                    i + 1
                )));
            }
        };
    }
    Ok(query)
}

impl Conn {
    /// Is this the Postgres backend (`db._is_postgres`)?
    #[must_use]
    pub const fn is_postgres(&self) -> bool {
        matches!(self, Self::Postgres(_))
    }

    /// `conn.execute(sql, params)`: SQLite dialect, `?` placeholders.
    ///
    /// # Errors
    /// Translation, binding or the driver.
    pub async fn execute(&mut self, sql: &str, params: &[Param]) -> Result<Cursor, StorageError> {
        match self {
            Self::Postgres(conn) => {
                let text = pg_render(&translate(sql)?, params)?;
                let mut rows = Vec::new();
                let mut affected = 0_i64;
                {
                    let mut stream = sqlx::raw_sql(&text).fetch_many(&mut **conn);
                    while let Some(item) = stream.try_next().await? {
                        match item {
                            Either::Left(done) => {
                                affected = i64::try_from(done.rows_affected()).unwrap_or(i64::MAX);
                            }
                            Either::Right(row) => rows.push(row),
                        }
                    }
                }
                let rows = pg_rows(&rows)?;
                Ok(Cursor {
                    rows,
                    rowcount: affected,
                    last_insert_id: None,
                    postgres: true,
                })
            }
            Self::Sqlite(conn) => Self::sqlite_execute(conn, sql, params).await,
            Self::SqliteOwned(conn) => Self::sqlite_execute(conn, sql, params).await,
        }
    }

    async fn sqlite_execute(
        conn: &mut sqlx::SqliteConnection,
        sql: &str,
        params: &[Param],
    ) -> Result<Cursor, StorageError> {
        let head = sql.trim_start().to_ascii_lowercase();
        let returns_rows = ["select", "pragma", "with", "values"]
            .iter()
            .any(|k| head.starts_with(k))
            || head.contains("returning");
        let query = sqlite_bind(sqlx::query(sql), params)?;
        if returns_rows {
            let rows = query.fetch_all(&mut *conn).await?;
            let rowcount = if head.contains("returning") {
                i64::try_from(rows.len()).unwrap_or(i64::MAX)
            } else {
                -1
            };
            return Ok(Cursor {
                rows: sqlite_rows(&rows)?,
                rowcount,
                last_insert_id: None,
                postgres: false,
            });
        }
        let done = query.execute(&mut *conn).await?;
        Ok(Cursor {
            rows: Vec::new(),
            rowcount: i64::try_from(done.rows_affected()).unwrap_or(i64::MAX),
            last_insert_id: Some(done.last_insert_rowid()),
            postgres: false,
        })
    }

    /// `conn.executemany(sql, seq)`.
    ///
    /// # Errors
    /// As [`Conn::execute`], at the first failing row.
    pub async fn executemany(
        &mut self,
        sql: &str,
        seq: &[Vec<Param>],
    ) -> Result<i64, StorageError> {
        let mut total = 0;
        for params in seq {
            total += self.execute(sql, params).await?.rowcount.max(0);
        }
        Ok(total)
    }

    /// `db.transaction(conn)`: `BEGIN IMMEDIATE` (SQLite) / `BEGIN` (Postgres), the body, then
    /// `COMMIT` — or `ROLLBACK` and the body's error.
    ///
    /// # Errors
    /// The body's, or the driver's.
    pub async fn transaction<T, F>(&mut self, body: F) -> Result<T, StorageError>
    where
        F: for<'c> FnOnce(
            &'c mut Self,
        ) -> std::pin::Pin<
            Box<dyn std::future::Future<Output = Result<T, StorageError>> + Send + 'c>,
        >,
    {
        let begin = if self.is_postgres() {
            "BEGIN"
        } else {
            "BEGIN IMMEDIATE"
        };
        self.execute(begin, &[]).await?;
        match body(self).await {
            Ok(value) => {
                self.execute("COMMIT", &[]).await?;
                Ok(value)
            }
            Err(e) => {
                let _ = self.execute("ROLLBACK", &[]).await;
                Err(e)
            }
        }
    }

    /// `db.schema_version(conn)`: `PRAGMA user_version` on SQLite, the one-row
    /// `schema_version` table on Postgres (created at 0 if absent).
    ///
    /// # Errors
    /// The driver's.
    pub async fn schema_version(&mut self) -> Result<i64, StorageError> {
        if !self.is_postgres() {
            let cur = self.execute("PRAGMA user_version", &[]).await?;
            return Ok(cur
                .fetchone()
                .and_then(|r| r.at(0))
                .and_then(DbValue::as_i64)
                .unwrap_or(0));
        }
        self.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (version integer not null)",
            &[],
        )
        .await?;
        let cur = self
            .execute("SELECT version FROM schema_version", &[])
            .await?;
        match cur
            .fetchone()
            .and_then(|r| r.at(0))
            .and_then(DbValue::as_i64)
        {
            Some(v) => Ok(v),
            None => {
                self.execute("INSERT INTO schema_version (version) VALUES (0)", &[])
                    .await?;
                Ok(0)
            }
        }
    }

    async fn set_schema_version(&mut self, version: i64) -> Result<(), StorageError> {
        if self.is_postgres() {
            self.execute(
                "UPDATE schema_version SET version = ?",
                &[Param::Int(version)],
            )
            .await?;
        } else {
            self.execute(&format!("PRAGMA user_version={version}"), &[])
                .await?;
        }
        Ok(())
    }

    /// `db.migrate(conn)`: apply every pending migration, each in its own transaction.
    ///
    /// # Errors
    /// The first failing statement (its migration rolled back).
    pub async fn migrate(&mut self) -> Result<i64, StorageError> {
        let mut have = self.schema_version().await?;
        for (version, statements) in MIGRATIONS {
            if *version <= have {
                continue;
            }
            let version = *version;
            self.transaction(move |conn| {
                Box::pin(async move {
                    for statement in *statements {
                        conn.execute(statement, &[]).await?;
                    }
                    conn.set_schema_version(version).await
                })
            })
            .await?;
            have = version;
        }
        Ok(have)
    }
}
