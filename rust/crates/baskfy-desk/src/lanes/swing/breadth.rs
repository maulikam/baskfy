//! Ports `app/breadth_source.py` (M14 §1): which 20-DMA breadth number the weekly plan's cash
//! bands see — the pipeline's (`market_health_daily.pct_above_20dma` over `DESK_UNIVERSE_SLUG`)
//! when its membership is the scan's, symbol for symbol, else the scan's own, and always why.
//!
//! The decision ([`breadth_for_plan`]) is pure over a [`PipelineBreadth`] reader, so it is
//! tested without a database; [`PgPipeline`] is the reader over `SCREENER_DATABASE_URL` with
//! Python's two statements. Neither branch guesses: a different universe, a missing row or an
//! unreachable pipeline each fall back to the scan's figure and say so on the plan.

use std::collections::BTreeSet;

use async_trait::async_trait;
use chrono::NaiveDate;

use crate::wire::{PyDict, PyValue};

/// `_SQL`.
pub const BREADTH_SQL: &str = "select m.pct_above_20dma, m.constituent_count \
     from market_health_daily m join index_def d on d.id = m.index_id \
     where d.slug = $1 and m.date = $2";

/// `_MEMBERS_SQL`.
pub const MEMBERS_SQL: &str = "select i.symbol from index_member_daily m \
     join instrument i on i.id = m.instrument_id \
     join index_def d on d.id = m.index_id \
     where d.slug = $1 and m.date = $2";

/// A pipeline read that raised: the exception's class name, as `exc.__class__.__name__`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PipelineError(pub String);

/// `pipeline_breadth(on, slug)`: the value (or `None`) and the membership it was measured over.
#[async_trait]
pub trait PipelineBreadth: Send + Sync {
    /// One date's breadth and members.
    async fn read(
        &self,
        on: NaiveDate,
        slug: &str,
    ) -> Result<(Option<f64>, BTreeSet<String>), PipelineError>;
}

/// The reader over the screener's Postgres (`psycopg.connect(SCREENER_DSN)`).
#[derive(Debug, Clone)]
pub struct PgPipeline {
    /// `SCREENER_DATABASE_URL`.
    pub dsn: String,
}

/// `exc.__class__.__name__` for an error raised by a statement, as psycopg names it: the
/// server's SQLSTATE picks the `psycopg.errors` class (the common ones are named here; any other
/// server error falls back to the base `DatabaseError`), and a transport failure mid-statement
/// is psycopg's `OperationalError`.
fn class_of(e: &sqlx::Error) -> PipelineError {
    let name = match e {
        sqlx::Error::Database(db) => match db.code().as_deref() {
            Some("42P01") => "UndefinedTable",
            Some("42703") => "UndefinedColumn",
            Some("42883") => "UndefinedFunction",
            Some("42501") => "InsufficientPrivilege",
            Some("57014") => "QueryCanceled",
            Some("57P01") => "AdminShutdown",
            _ => "DatabaseError",
        },
        _ => "OperationalError",
    };
    PipelineError(name.to_owned())
}

/// `psycopg.connect(...)` failing to connect: psycopg raises `OperationalError` for every
/// connection it cannot make, including one the server refused (a bad password, a missing
/// database) and an option value libpq rejects at connect time (`port=notaport`,
/// `sslmode=bogus`), so a connect error is never classed by its SQLSTATE.
fn connect_class(_e: &sqlx::Error) -> PipelineError {
    PipelineError("OperationalError".to_owned())
}

/// libpq's keywords (`PQconndefaults()` of the libpq psycopg ships, 18.0): a key/value pair or a
/// URI query parameter naming anything else fails `PQconninfoParse`.
pub const LIBPQ_KEYWORDS: [&str; 50] = [
    "application_name",
    "channel_binding",
    "client_encoding",
    "connect_timeout",
    "dbname",
    "fallback_application_name",
    "gssdelegation",
    "gssencmode",
    "gsslib",
    "host",
    "hostaddr",
    "keepalives",
    "keepalives_count",
    "keepalives_idle",
    "keepalives_interval",
    "krbsrvname",
    "load_balance_hosts",
    "max_protocol_version",
    "min_protocol_version",
    "oauth_client_id",
    "oauth_client_secret",
    "oauth_issuer",
    "oauth_scope",
    "options",
    "passfile",
    "password",
    "port",
    "replication",
    "require_auth",
    "requirepeer",
    "scram_client_key",
    "scram_server_key",
    "service",
    "ssl_max_protocol_version",
    "ssl_min_protocol_version",
    "sslcert",
    "sslcertmode",
    "sslcompression",
    "sslcrl",
    "sslcrldir",
    "sslkey",
    "sslkeylogfile",
    "sslmode",
    "sslnegotiation",
    "sslpassword",
    "sslrootcert",
    "sslsni",
    "target_session_attrs",
    "tcp_user_timeout",
    "user",
];

/// `PQconninfoParse` refused the string (psycopg's `ProgrammingError`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ConninfoError;

/// libpq's URI percent-decoding (`conninfo_uri_decode`): `%` must be followed by two hex digits,
/// and `%00` is forbidden.
fn uri_decode(text: &str) -> Result<String, ConninfoError> {
    let bytes = text.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' {
            let hex = |b: u8| char::from(b).to_digit(16);
            let (Some(hi), Some(lo)) = (
                bytes.get(i + 1).copied().and_then(hex),
                bytes.get(i + 2).copied().and_then(hex),
            ) else {
                return Err(ConninfoError);
            };
            let byte = u8::try_from(hi * 16 + lo).map_err(|_| ConninfoError)?;
            if byte == 0 {
                return Err(ConninfoError);
            }
            out.push(byte);
            i += 3;
        } else {
            out.push(bytes[i]);
            i += 1;
        }
    }
    String::from_utf8(out).map_err(|_| ConninfoError)
}

/// The connection parameters libpq parsed out of a conninfo string (keyword → value).
pub type Conninfo = Vec<(String, String)>;

/// `PQconninfoParse(dsn)`: the parameters, or `Err` where libpq refuses the string — which
/// psycopg raises as `ProgrammingError` before any connection is tried (`missing "=" after …`,
/// `invalid percent-encoded token`, `invalid URI query parameter`, `unterminated quoted string`,
/// `invalid connection option`, an unclosed IPv6 bracket). Values are not validated here: libpq
/// rejects a non-numeric port or an unknown sslmode only when it connects, which psycopg raises
/// as `OperationalError`.
///
/// # Errors
/// The string is not a conninfo libpq can parse.
pub fn libpq_parse(dsn: &str) -> Result<Conninfo, ConninfoError> {
    for prefix in ["postgresql://", "postgres://"] {
        if let Some(rest) = dsn.strip_prefix(prefix) {
            return libpq_parse_uri(rest);
        }
    }
    libpq_parse_kv(dsn)
}

fn libpq_parse_uri(rest: &str) -> Result<Conninfo, ConninfoError> {
    let mut out: Conninfo = Vec::new();
    let (before_query, query) = match rest.split_once('?') {
        Some((a, q)) => (a, Some(q)),
        None => (rest, None),
    };
    let (authority, path) = match before_query.split_once('/') {
        Some((a, p)) => (a, Some(p)),
        None => (before_query, None),
    };
    let hosts = match authority.rsplit_once('@') {
        Some((userinfo, hosts)) => {
            let (user, password) = match userinfo.split_once(':') {
                Some((u, p)) => (u, Some(p)),
                None => (userinfo, None),
            };
            if !user.is_empty() {
                out.push(("user".to_owned(), uri_decode(user)?));
            }
            if let Some(p) = password.filter(|p| !p.is_empty()) {
                out.push(("password".to_owned(), uri_decode(p)?));
            }
            hosts
        }
        None => authority,
    };
    let mut host_list = Vec::new();
    let mut port_list = Vec::new();
    for spec in hosts.split(',') {
        let (host, port) = if let Some(v6) = spec.strip_prefix('[') {
            let (h, after) = v6.split_once(']').ok_or(ConninfoError)?;
            (h, after.strip_prefix(':'))
        } else {
            match spec.split_once(':') {
                Some((h, p)) => (h, Some(p)),
                None => (spec, None),
            }
        };
        host_list.push(uri_decode(host)?);
        port_list.push(port.map(uri_decode).transpose()?.unwrap_or_default());
    }
    if host_list.iter().any(|h| !h.is_empty()) {
        out.push(("host".to_owned(), host_list.join(",")));
    }
    if port_list.iter().any(|p| !p.is_empty()) {
        out.push(("port".to_owned(), port_list.join(",")));
    }
    if let Some(db) = path.filter(|p| !p.is_empty()) {
        out.push(("dbname".to_owned(), uri_decode(db)?));
    }
    if let Some(q) = query {
        for param in q.split('&') {
            let (key, value) = param.split_once('=').ok_or(ConninfoError)?;
            if value.contains('=') {
                return Err(ConninfoError);
            }
            let (key, value) = (uri_decode(key)?, uri_decode(value)?);
            // libpq's one alias: `ssl=true` is `sslmode=require`.
            let (key, value) = if key == "ssl" && value == "true" {
                ("sslmode".to_owned(), "require".to_owned())
            } else {
                (key, value)
            };
            if !LIBPQ_KEYWORDS.contains(&key.as_str()) {
                return Err(ConninfoError);
            }
            out.push((key, value));
        }
    }
    Ok(out)
}

fn libpq_parse_kv(dsn: &str) -> Result<Conninfo, ConninfoError> {
    let mut out: Conninfo = Vec::new();
    let mut chars = dsn.chars().peekable();
    loop {
        while chars.peek().is_some_and(|c| c.is_whitespace()) {
            chars.next();
        }
        if chars.peek().is_none() {
            return Ok(out);
        }
        let mut key = String::new();
        while let Some(&c) = chars.peek() {
            if c == '=' || c.is_whitespace() {
                break;
            }
            key.push(c);
            chars.next();
        }
        while chars.peek().is_some_and(|c| c.is_whitespace()) {
            chars.next();
        }
        if chars.next() != Some('=') {
            return Err(ConninfoError);
        }
        while chars.peek().is_some_and(|c| c.is_whitespace()) {
            chars.next();
        }
        let mut value = String::new();
        if chars.peek() == Some(&'\'') {
            chars.next();
            loop {
                match chars.next() {
                    None => return Err(ConninfoError),
                    Some('\\') => value.push(chars.next().ok_or(ConninfoError)?),
                    Some('\'') => break,
                    Some(c) => value.push(c),
                }
            }
        } else {
            while let Some(&c) = chars.peek() {
                if c.is_whitespace() {
                    break;
                }
                chars.next();
                if c == '\\' {
                    if let Some(n) = chars.next() {
                        value.push(n);
                    }
                } else {
                    value.push(c);
                }
            }
        }
        if !LIBPQ_KEYWORDS.contains(&key.as_str()) {
            return Err(ConninfoError);
        }
        out.push((key, value));
    }
}

/// The connect options for a conninfo libpq accepted. A URI goes to sqlx as written; a key/value
/// string (which sqlx cannot read) is mapped keyword by keyword onto the options sqlx has. A
/// value sqlx refuses is a connect-time failure, as libpq's own value checks are.
fn connect_options(
    dsn: &str,
    parsed: &Conninfo,
) -> Result<sqlx::postgres::PgConnectOptions, sqlx::Error> {
    use std::str::FromStr as _;
    if dsn.starts_with("postgresql://") || dsn.starts_with("postgres://") {
        return sqlx::postgres::PgConnectOptions::from_str(dsn);
    }
    let mut o = sqlx::postgres::PgConnectOptions::new();
    for (key, value) in parsed {
        o = match key.as_str() {
            "host" | "hostaddr" => o.host(value),
            "port" => o.port(
                value
                    .parse::<u16>()
                    .map_err(|e| sqlx::Error::Configuration(Box::new(e)))?,
            ),
            "dbname" => o.database(value),
            "user" => o.username(value),
            "password" => o.password(value),
            "application_name" => o.application_name(value),
            "options" => o.options([("options", value.as_str())]),
            "sslmode" => o.ssl_mode(
                sqlx::postgres::PgSslMode::from_str(value)
                    .map_err(|e| sqlx::Error::Configuration(Box::new(e)))?,
            ),
            "sslrootcert" => o.ssl_root_cert(value.as_str()),
            _ => o,
        };
    }
    Ok(o)
}

/// `exc.__class__.__name__` for `psycopg.connect(dsn)` failing before a statement runs: a string
/// libpq cannot parse is `ProgrammingError`; anything else that stops the connection is
/// `OperationalError`.
///
/// # Errors
/// The class name of the failure.
pub async fn connect_like_psycopg(dsn: &str) -> Result<sqlx::PgConnection, PipelineError> {
    use sqlx::ConnectOptions as _;
    let parsed =
        libpq_parse(dsn).map_err(|ConninfoError| PipelineError("ProgrammingError".to_owned()))?;
    let options = connect_options(dsn, &parsed).map_err(|e| connect_class(&e))?;
    options.connect().await.map_err(|e| connect_class(&e))
}

#[async_trait]
impl PipelineBreadth for PgPipeline {
    async fn read(
        &self,
        on: NaiveDate,
        slug: &str,
    ) -> Result<(Option<f64>, BTreeSet<String>), PipelineError> {
        use sqlx::Row;
        let mut conn = connect_like_psycopg(&self.dsn).await?;
        let row = sqlx::query(BREADTH_SQL)
            .bind(slug)
            .bind(on)
            .fetch_optional(&mut conn)
            .await
            .map_err(|e| class_of(&e))?;
        let members: Vec<sqlx::postgres::PgRow> = sqlx::query(MEMBERS_SQL)
            .bind(slug)
            .bind(on)
            .fetch_all(&mut conn)
            .await
            .map_err(|e| class_of(&e))?;
        let members: BTreeSet<String> = members
            .iter()
            .filter_map(|r| r.try_get::<String, _>(0).ok())
            .collect();
        let value = row.and_then(|r| {
            r.try_get::<Option<rust_decimal::Decimal>, _>(0)
                .ok()
                .flatten()
                .and_then(|d| d.to_string().parse::<f64>().ok())
                .or_else(|| r.try_get::<Option<f64>, _>(0).ok().flatten())
        });
        Ok((value, members))
    }
}

/// `breadth_for_plan(scan_breadth, symbols, on)`: decide which number the cash bands see, and
/// record why. Keys in Python's order: `value`, `source`, `scan_value`, `pipeline_value`,
/// `universe`, `reason`.
pub async fn breadth_for_plan(
    scan_breadth: f64,
    symbols: &BTreeSet<String>,
    on: NaiveDate,
    universe: &str,
    reader: &dyn PipelineBreadth,
) -> PyDict {
    let mut value = scan_breadth;
    let mut source = "scan";
    let pipeline_value;
    let reason;
    match reader.read(on, universe).await {
        Err(PipelineError(class)) => {
            let mut out = PyDict::new();
            out.insert("value", PyValue::Float(scan_breadth));
            out.insert("source", PyValue::Str("scan".to_owned()));
            out.insert("scan_value", PyValue::Float(scan_breadth));
            out.insert("pipeline_value", PyValue::None);
            out.insert("universe", PyValue::Str(universe.to_owned()));
            out.insert(
                "reason",
                PyValue::Str(format!(
                    "pipeline unreachable ({class}); used the scan's own"
                )),
            );
            return out;
        }
        Ok((pv, members)) => {
            pipeline_value = pv;
            if pv.is_none() {
                reason = format!("no market_health_daily row for {on}; used the scan's own");
            } else if &members != symbols {
                let only_scan = symbols.difference(&members).count();
                let only_index = members.difference(symbols).count();
                reason = format!(
                    "scan universe differs from {universe} on {on} ({only_scan} only in the \
                     scan, {only_index} only in the index); used the scan's own"
                );
            } else {
                value = pv.unwrap_or(scan_breadth);
                source = "pipeline";
                reason = format!("same {} symbols as {universe} on {on}", members.len());
            }
        }
    }
    let mut out = PyDict::new();
    out.insert("value", PyValue::Float(value));
    out.insert("source", PyValue::Str(source.to_owned()));
    out.insert("scan_value", PyValue::Float(scan_breadth));
    out.insert(
        "pipeline_value",
        pipeline_value.map_or(PyValue::None, PyValue::Float),
    );
    out.insert("universe", PyValue::Str(universe.to_owned()));
    out.insert("reason", PyValue::Str(reason));
    out
}

#[cfg(test)]
mod tests;
