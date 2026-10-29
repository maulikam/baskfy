//! What the write routes of the sleeve routers share (lane writes-sleeves): reading the body,
//! the write pool, the clock, and the **Celery producer** (`baskfy_api.queue.CeleryTaskQueue`,
//! `baskfy_api.deferred_publish`).
//!
//! **The producer.** Python's API publishes a task by name with kombu's Redis transport: the
//! queue's binding is added to `_kombu.binding.<queue>` and the message — protocol 2, JSON, the
//! body base64-encoded — is `LPUSH`ed onto the list named after the queue, which the producer's
//! routing table ([`PRODUCER_TASK_ROUTES`], a copy of `queue.PRODUCER_TASK_ROUTES`) resolves from
//! the task name. [`send_task`] writes the same two keys with the same message, in one Lua script.
//! The task id is a fresh UUID4; `origin` is `gen<pid>@<host>` as Celery spells it. Nothing here
//! names a broker of orders: these are the worker's queues (scans, backtests), never the desk's.
//!
//! **After commit.** `deferred_publish.defer_task_publish` publishes once the request's
//! transaction has committed and then stamps the row's `task_id` in a short second transaction;
//! [`publish_after_commit`] is that, called by the handler after `session::run` returned.

use axum::Extension;
use axum::body::Body;
use axum::extract::Request;
use axum::http::header;
use base64::Engine;
use baskfy_db::OrderedJson;
use baskfy_providers::ratelimit::ScriptValue;
use baskfy_providers::secret::Secret;
use std::cmp::Ordering;

use chrono::{DateTime, NaiveDate, Timelike, Utc};
use rust_decimal::Decimal;

use crate::routes::sleeves::common::db;
use crate::routes::sleeves::pyd::{PV, PyDec, dec_const};
use crate::session::Tx;

use crate::problems::{Problem, internal_error};
use crate::session::{self, WritePool};

/// The largest body a write route reads (the session routes' bound).
const MAX_BODY_BYTES: usize = 16 * 1024 * 1024;

/// The request's `Content-Type` and its body, read whole (`await request.body()`).
///
/// # Errors
/// Starlette's 400 when the body cannot be read.
pub(crate) async fn read_body(
    request: Request<Body>,
) -> Result<(Option<String>, Vec<u8>), Problem> {
    let content_type = request
        .headers()
        .get(header::CONTENT_TYPE)
        .map(|v| String::from_utf8_lossy(v.as_bytes()).into_owned());
    let bytes = axum::body::to_bytes(request.into_body(), MAX_BODY_BYTES)
        .await
        .map_err(|_| crate::problems::framework(400, "There was an error parsing the body"))?;
    Ok((content_type, bytes.to_vec()))
}

/// The write pool a write route was handed.
///
/// # Errors
/// The 500 catch-all when the process has none (a database-free test state).
pub(crate) fn pool(pool: Option<Extension<WritePool>>) -> Result<WritePool, Problem> {
    pool.map(|Extension(p)| p).ok_or_else(internal_error)
}

/// `datetime.now(tz=UTC)` at the microsecond Python's clock and Postgres keep.
pub(crate) fn now_micros() -> DateTime<Utc> {
    let t = crate::entitlements::utc_now();
    t.with_nanosecond(t.nanosecond() / 1000 * 1000).unwrap_or(t)
}

/// A random UUID4, hyphenated (`str(uuid.uuid4())`).
pub(crate) fn uuid4() -> String {
    let mut b: [u8; 16] = rand::random();
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    let hex: String = b.iter().map(|x| format!("{x:02x}")).collect();
    format!(
        "{}-{}-{}-{}-{}",
        &hex[0..8],
        &hex[8..12],
        &hex[12..16],
        &hex[16..20],
        &hex[20..32]
    )
}

// ---------------------------------------------------------------------------------------------
// The Celery producer
// ---------------------------------------------------------------------------------------------

/// `queue.PRODUCER_TASK_ROUTES`, in Python's order (the first matching glob wins).
pub(crate) const PRODUCER_TASK_ROUTES: [(&str, &str); 21] = [
    ("baskfy.ingest.*", "ingest"),
    ("baskfy.eq_bars.*", "ingest"),
    ("baskfy.compute.*", "compute"),
    ("baskfy.backtest.*", "backtest"),
    ("baskfy.pipeline.*", "default"),
    ("baskfy.ops.*", "default"),
    ("baskfy.alerts.*", "default"),
    ("baskfy.desk.*", "default"),
    ("baskfy.cb.*", "compute"),
    ("baskfy.portfolio.*", "compute"),
    ("baskfy.swing.scan_now", "compute"),
    ("baskfy.swing.scan_after_login", "compute"),
    ("baskfy.swing.scan_sweep", "default"),
    ("baskfy.kite.*", "compute"),
    ("baskfy.vbt.backtest", "compute"),
    ("baskfy.overlap.catalyst_scan", "compute"),
    ("baskfy.fno.retest", "compute"),
    ("baskfy.vbt.rescan", "compute"),
    ("baskfy.vbt.rescan_sweep", "default"),
    ("baskfy.twt.scan", "compute"),
    ("baskfy.twt.scan_publish", "default"),
];

/// `queue.DEFAULT_QUEUE`.
pub(crate) const DEFAULT_QUEUE: &str = "default";

/// The queue Celery's router picks for `name` (`fnmatch` globs, first match).
pub(crate) fn queue_for(name: &str) -> &'static str {
    for (pattern, queue) in PRODUCER_TASK_ROUTES {
        let hit = match pattern.strip_suffix('*') {
            Some(prefix) => name.starts_with(prefix),
            None => name == pattern,
        };
        if hit {
            return queue;
        }
    }
    DEFAULT_QUEUE
}

/// kombu's binding member: `routing_key \x06\x16 pattern \x06\x16 queue`.
fn binding_member(queue: &str) -> String {
    format!("{queue}\u{6}\u{16}\u{6}\u{16}{queue}")
}

/// `json.dumps(value)` (default separators).
fn py_json(v: &OrderedJson, out: &mut String) {
    match v {
        OrderedJson::Null => out.push_str("null"),
        OrderedJson::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        OrderedJson::Int(i) => out.push_str(&i.to_string()),
        OrderedJson::UInt(u) => out.push_str(&u.to_string()),
        OrderedJson::BigInt(s) => out.push_str(s),
        OrderedJson::Float(f) => out.push_str(&baskfy_core::py::float_repr(*f)),
        OrderedJson::Str(s) => out.push_str(&ascii_json_str(s)),
        OrderedJson::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                py_json(item, out);
            }
            out.push(']');
        }
        OrderedJson::Object(entries) => {
            out.push('{');
            for (i, (k, item)) in entries.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                out.push_str(&ascii_json_str(k));
                out.push_str(": ");
                py_json(item, out);
            }
            out.push('}');
        }
    }
}

/// `json.dumps(str)` with `ensure_ascii=True`.
fn ascii_json_str(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if c.is_ascii() && !c.is_ascii_control() => out.push(c),
            c => {
                let mut units = [0_u16; 2];
                for u in c.encode_utf16(&mut units) {
                    out.push_str(&format!("\\u{u:04x}"));
                }
            }
        }
    }
    out.push('"');
    out
}

/// `repr(list(args))` for Celery's `argsrepr` (strings, ints and booleans are all it carries).
fn args_repr(args: &[OrderedJson]) -> String {
    let parts: Vec<String> = args
        .iter()
        .map(crate::routes::sleeves::common::py_repr_json)
        .collect();
    format!("[{}]", parts.join(", "))
}

/// `gen<pid>@<host>` — Celery's `origin` (`anon_nodename()`).
fn origin() -> String {
    let host = std::env::var("HOSTNAME")
        .ok()
        .filter(|h| !h.is_empty())
        .unwrap_or_else(|| "localhost".to_owned());
    format!("gen{}@{host}", std::process::id())
}

/// The message `send_task(name, args=args)` publishes, and its task id.
pub(crate) fn celery_message(name: &str, args: &[OrderedJson]) -> (String, String, &'static str) {
    let task_id = uuid4();
    let queue = queue_for(name);
    let mut body = String::new();
    py_json(
        &OrderedJson::Array(vec![
            OrderedJson::Array(args.to_vec()),
            OrderedJson::Object(Vec::new()),
            OrderedJson::Object(vec![
                ("callbacks".to_owned(), OrderedJson::Null),
                ("errbacks".to_owned(), OrderedJson::Null),
                ("chain".to_owned(), OrderedJson::Null),
                ("chord".to_owned(), OrderedJson::Null),
            ]),
        ]),
        &mut body,
    );
    let s = |v: &str| OrderedJson::Str(v.to_owned());
    let message = OrderedJson::Object(vec![
        (
            "body".to_owned(),
            s(&base64::engine::general_purpose::STANDARD.encode(body.as_bytes())),
        ),
        ("content-encoding".to_owned(), s("utf-8")),
        ("content-type".to_owned(), s("application/json")),
        (
            "headers".to_owned(),
            OrderedJson::Object(vec![
                ("lang".to_owned(), s("py")),
                ("task".to_owned(), s(name)),
                ("id".to_owned(), s(&task_id)),
                ("shadow".to_owned(), OrderedJson::Null),
                ("eta".to_owned(), OrderedJson::Null),
                ("expires".to_owned(), OrderedJson::Null),
                ("group".to_owned(), OrderedJson::Null),
                ("group_index".to_owned(), OrderedJson::Null),
                ("retries".to_owned(), OrderedJson::Int(0)),
                (
                    "timelimit".to_owned(),
                    OrderedJson::Array(vec![OrderedJson::Null, OrderedJson::Null]),
                ),
                ("root_id".to_owned(), s(&task_id)),
                ("parent_id".to_owned(), OrderedJson::Null),
                ("argsrepr".to_owned(), s(&args_repr(args))),
                ("kwargsrepr".to_owned(), s("{}")),
                ("origin".to_owned(), s(&origin())),
                ("ignore_result".to_owned(), OrderedJson::Bool(false)),
                ("replaced_task_nesting".to_owned(), OrderedJson::Int(0)),
                ("stamped_headers".to_owned(), OrderedJson::Null),
                ("stamps".to_owned(), OrderedJson::Object(Vec::new())),
            ]),
        ),
        (
            "properties".to_owned(),
            OrderedJson::Object(vec![
                ("correlation_id".to_owned(), s(&task_id)),
                ("reply_to".to_owned(), s(&uuid4())),
                ("delivery_mode".to_owned(), OrderedJson::Int(2)),
                (
                    "delivery_info".to_owned(),
                    OrderedJson::Object(vec![
                        ("exchange".to_owned(), s("")),
                        ("routing_key".to_owned(), s(queue)),
                    ]),
                ),
                ("priority".to_owned(), OrderedJson::Int(0)),
                ("body_encoding".to_owned(), s("base64")),
                ("delivery_tag".to_owned(), s(&uuid4())),
            ]),
        ),
    ]);
    let mut text = String::new();
    py_json(&message, &mut text);
    (task_id, text, queue)
}

/// The publish: the queue's binding, then the message on the queue's list.
const PUBLISH_SCRIPT: &str =
    "redis.call('SADD', KEYS[1], ARGV[1]); redis.call('LPUSH', KEYS[2], ARGV[2]); return 1";

/// `queue.send_task(name, args)`: the task id, or why it could not be published (Python's
/// `send_task` raising).
///
/// # Errors
/// Redis unreachable or refusing the script.
pub(crate) async fn send_task(
    url: &Secret,
    name: &str,
    args: &[OrderedJson],
) -> Result<String, String> {
    let (task_id, message, queue) = celery_message(name, args);
    let keys = [format!("_kombu.binding.{queue}"), queue.to_owned()];
    match crate::cache::eval(
        url,
        PUBLISH_SCRIPT,
        &keys,
        &[binding_member(queue), message],
    )
    .await
    {
        Ok(ScriptValue::Int(_)) => Ok(task_id),
        Ok(other) => Err(format!("unexpected reply {other:?}")),
        Err(e) => Err(e),
    }
}

/// Whether the API has a task queue at all (`build_task_queue` returns `None` without a Redis
/// URL).
pub(crate) fn has_queue(url: &Secret) -> bool {
    !url.expose().is_empty()
}

/// `drain_deferred_publishes` for one row: publish after the commit, then stamp `task_id` on
/// `table`'s row in a short transaction of its own. A failed publish is logged and leaves the row
/// for the worker's sweep, as Python's does.
pub(crate) async fn publish_after_commit(
    url: &Secret,
    pool: &WritePool,
    table: &'static str,
    task_name: &'static str,
    run_id: i64,
) {
    match send_task(url, task_name, &[OrderedJson::Int(run_id)]).await {
        Ok(task_id) => {
            let stamped = session::run(pool, move |tx| {
                Box::pin(async move {
                    sqlx::query(&format!("UPDATE {table} SET task_id = $1 WHERE id = $2"))
                        .bind(&task_id)
                        .bind(run_id)
                        .execute(&mut **tx)
                        .await
                        .map_err(crate::routes::sleeves::common::db)?;
                    Ok(())
                })
            })
            .await;
            if stamped.is_err() {
                crate::logging::warning(
                    "baskfy_api.deferred_publish",
                    "task id could not be stamped",
                    &[("run_id", &run_id.to_string())],
                );
            }
        }
        Err(e) => crate::logging::warning(
            "baskfy_api.deferred_publish",
            &format!("scan now {run_id} could not be published ({e}); the sweep will"),
            &[],
        ),
    }
}

// ---------------------------------------------------------------------------------------------
// "Scan now" (`swing_scan` / `twt_scan` / `vbt_scan`.request_scan)
// ---------------------------------------------------------------------------------------------

/// One sleeve's scan table and its rules.
#[derive(Debug, Clone, Copy)]
pub(crate) struct ScanSpec {
    /// The run table (`sw_scan_run`, `tw_scan_run`, `vb_scan_run`).
    pub(crate) table: &'static str,
    /// The `INSERT ... RETURNING id` for a new row: `$1` the user, `$2` `requested_at`.
    pub(crate) insert_sql: &'static str,
    /// The task the worker binds.
    pub(crate) task: &'static str,
    /// `*_scan_min_interval_seconds`.
    pub(crate) min_interval_seconds: i64,
    /// `*_scan_stale_after_seconds`.
    pub(crate) stale_after_seconds: i64,
}

/// `timedelta.total_seconds()` of a microsecond span.
#[allow(clippy::cast_precision_loss)]
fn total_seconds(micros: i64) -> f64 {
    micros as f64 / 1_000_000.0
}

/// `request_scan(...)`: the 409 for a scan on its way, the 429 for one inside the minute, else
/// the new `QUEUED` row (inserted in the request's transaction) — its id and `requested_at`.
///
/// # Errors
/// The two refusals, or the 500 for a database failure.
pub(crate) async fn request_scan(
    pool: &WritePool,
    user_id: i64,
    spec: ScanSpec,
) -> Result<(i64, DateTime<Utc>), Problem> {
    use crate::problems::ProblemType;
    use sqlx::Row;
    let now = now_micros();
    session::run(pool, move |tx| {
        Box::pin(async move {
            let stale_cut = now - chrono::Duration::seconds(spec.stale_after_seconds);
            let running = sqlx::query(&format!(
                "SELECT id, status FROM {} WHERE user_id = $1 AND status IN ('QUEUED', 'RUNNING') \
                 AND requested_at > $2 ORDER BY requested_at DESC, id DESC LIMIT 1",
                spec.table
            ))
            .bind(user_id)
            .bind(stale_cut)
            .fetch_optional(&mut **tx)
            .await
            .map_err(db)?;
            if let Some(row) = running {
                let id: i64 = row.try_get("id").map_err(db)?;
                let status: String = row.try_get("status").map_err(db)?;
                return Err(Problem::new(
                    ProblemType::ScanInFlight,
                    format!(
                        "Scan {id} is {}; its result is on its way.",
                        status.to_lowercase()
                    ),
                )
                .with("run_id", OrderedJson::Int(id))
                .with("status_of_run", OrderedJson::Str(status)));
            }
            let newest = sqlx::query(&format!(
                "SELECT id, requested_at FROM {} WHERE user_id = $1 \
                 ORDER BY requested_at DESC, id DESC LIMIT 1",
                spec.table
            ))
            .bind(user_id)
            .fetch_optional(&mut **tx)
            .await
            .map_err(db)?;
            if let Some(row) = newest {
                let id: i64 = row.try_get("id").map_err(db)?;
                let at: DateTime<Utc> = row.try_get("requested_at").map_err(db)?;
                let min_interval = chrono::Duration::seconds(spec.min_interval_seconds);
                if at > now - min_interval {
                    let elapsed = (now - at).num_microseconds().unwrap_or(0);
                    let wait = min_interval.num_microseconds().unwrap_or(0) - elapsed;
                    #[allow(clippy::cast_possible_truncation)]
                    let seconds = ((total_seconds(wait) + 0.999).trunc() as i64).max(1);
                    #[allow(clippy::cast_possible_truncation)]
                    let ago = total_seconds(elapsed).trunc() as i64;
                    return Err(Problem::new(
                        ProblemType::RateLimited,
                        format!(
                            "A scan was requested {ago} s ago; one a minute is the limit. \
                             Try again in {seconds} s."
                        ),
                    )
                    .header("Retry-After", seconds.to_string())
                    .with("retry_after", OrderedJson::Int(seconds))
                    .with("run_id", OrderedJson::Int(id)));
                }
            }
            let id: i64 = sqlx::query_scalar(spec.insert_sql)
                .bind(user_id)
                .bind(now)
                .fetch_one(&mut **tx)
                .await
                .map_err(db)?;
            Ok((id, now))
        })
    })
    .await
}

/// The `202` body: `{"run_id", "status": "QUEUED", "requested_at"}` (canonical JSON).
pub(crate) fn queued_response(
    run_id: i64,
    requested_at: DateTime<Utc>,
) -> axum::response::Response {
    use crate::routes::sleeves::common::{J, Obj, canonical_response};
    let mut response = canonical_response(
        &Obj::new()
            .put("run_id", J::Int(run_id))
            .put("status", J::s("QUEUED"))
            .put("requested_at", J::Dt(requested_at))
            .done(),
    );
    *response.status_mut() = axum::http::StatusCode::ACCEPTED;
    response
}

// ---------------------------------------------------------------------------------------------
// Net-change updates and audited settings
// ---------------------------------------------------------------------------------------------

/// A `Decimal` bound to a `numeric` column, or the 500 Python's driver would answer.
///
/// # Errors
/// The 500 catch-all.
pub(crate) fn bindable(d: &PyDec) -> Result<Decimal, Problem> {
    d.to_decimal().ok_or_else(|| {
        crate::logging::error(
            "baskfy_api.app",
            "unhandled error",
            &[("error", "numeric value out of range")],
        );
        crate::problems::internal_error()
    })
}

/// The SET list of one net-change `UPDATE`, with its binds.
#[derive(Default)]
pub(crate) struct Changes {
    pub(crate) sets: Vec<String>,
    pub(crate) binds: Vec<Bind>,
}

/// One bound value.
pub(crate) enum Bind {
    /// `numeric`.
    Dec(Decimal),
    /// `text`.
    Str(String),
    /// `date`.
    Date(Option<NaiveDate>),
    /// An integer.
    Int(i64),
    /// `boolean`.
    Bool(bool),
    /// `time`.
    Time(chrono::NaiveTime),
    /// A nullable `text`.
    OptStr(Option<String>),
    /// A nullable `numeric`.
    OptDec(Option<Decimal>),
}

impl Changes {
    /// `column = <bind>`.
    pub(crate) fn set(&mut self, column: &str, bind: Bind) {
        self.binds.push(bind);
        self.sets.push(format!("{column} = ${}", self.binds.len()));
    }

    /// `UPDATE table SET ..., updated_at = now() WHERE <key>` — nothing when nothing changed.
    pub(crate) async fn apply(
        self,
        tx: &mut Tx,
        table: &str,
        key: &str,
        key_binds: &[i64],
    ) -> Result<bool, Problem> {
        if self.sets.is_empty() {
            return Ok(false);
        }
        let mut sql = format!(
            "UPDATE {table} SET {}, updated_at = now() WHERE ",
            self.sets.join(", ")
        );
        let mut n = self.binds.len();
        let mut where_sql = key.to_owned();
        for _ in key_binds {
            n += 1;
            where_sql = where_sql.replacen('?', &format!("${n}"), 1);
        }
        sql.push_str(&where_sql);
        let mut q = sqlx::query(&sql);
        for b in self.binds {
            q = match b {
                Bind::Dec(d) => q.bind(d),
                Bind::Str(s) => q.bind(s),
                Bind::Date(d) => q.bind(d),
                Bind::Int(i) => q.bind(i),
                Bind::Bool(v) => q.bind(v),
                Bind::Time(t) => q.bind(t),
                Bind::OptStr(v) => q.bind(v),
                Bind::OptDec(v) => q.bind(v),
            };
        }
        for k in key_binds {
            q = q.bind(*k);
        }
        q.execute(&mut **tx).await.map_err(db)?;
        Ok(true)
    }
}

/// A stored setting, as Python's attribute holds it.
#[derive(Debug, Clone)]
pub(crate) enum Stored {
    /// A `Numeric` column.
    Dec(Decimal),
    /// An integer column.
    Int(i64),
    /// A string column.
    Str(String),
    /// A boolean column.
    Bool(bool),
    /// A `time` column.
    Time(chrono::NaiveTime),
}

impl Stored {
    /// `str(before)`.
    pub(crate) fn py_str(&self) -> String {
        match self {
            Self::Dec(d) => dec_const(*d).py_str(),
            Self::Int(i) => i.to_string(),
            Self::Str(s) => s.clone(),
            Self::Bool(b) => if *b { "True" } else { "False" }.to_owned(),
            Self::Time(t) => PV::Time(*t, false).py_str(),
        }
    }

    /// The ORM's `is_equal(new, old)` (numbers numerically).
    pub(crate) fn same_as(&self, value: &PV) -> bool {
        let numeric = |before: PyDec| {
            value
                .as_pydec()
                .is_some_and(|after| before.cmp_num(&after) == Ordering::Equal)
        };
        match (self, value) {
            (Self::Dec(d), _) => numeric(dec_const(*d)),
            (Self::Int(i), _) => numeric(dec_const(Decimal::from(*i))),
            (Self::Str(s), PV::Str(v)) => s == v,
            (Self::Bool(b), PV::Bool(v)) => b == v,
            (Self::Time(t), PV::Time(v, false)) => t == v,
            _ => false,
        }
    }
}

/// One audited setting change for `*_config_audit`.
pub(crate) struct AuditChange {
    /// The column.
    pub(crate) key: String,
    /// `str(before)`.
    pub(crate) old_value: String,
    /// `str(value)`.
    pub(crate) new_value: String,
}

/// What asyncpg's encoder raises for an integer no `bigint` can hold: the 500 catch-all, logged.
pub(crate) fn out_of_range() -> Problem {
    crate::logging::error(
        "baskfy_api.app",
        "unhandled error",
        &[("error", "value out of int64 range")],
    );
    internal_error()
}

/// The value to bind for a changed setting.
///
/// # Errors
/// 500 for a value no column can hold.
pub(crate) fn bind_for(value: &PV) -> Result<Bind, Problem> {
    Ok(match value {
        PV::Dec(d) => Bind::Dec(bindable(d)?),
        PV::Int(i) => Bind::Int(i.as_i64().ok_or_else(out_of_range)?),
        PV::Str(s) => Bind::Str(s.clone()),
        PV::Bool(b) => Bind::Bool(*b),
        PV::Time(t, false) => Bind::Time(*t),
        _ => return Err(crate::problems::internal_error()),
    })
}

/// `apply_patch`'s write half for one row: the audit list, and the net-change `UPDATE` (with
/// `updated_by`, which the ORM also writes only when it differs).
///
/// # Errors
/// 500 for a value no column can hold, or a database failure.
#[allow(clippy::too_many_arguments)]
pub(crate) async fn write_changes(
    tx: &mut Tx,
    table: &str,
    key: &str,
    key_binds: &[i64],
    stored: &[(String, Stored)],
    updated_by: Option<&str>,
    changes: &[(String, PV)],
    changed_by: &str,
    bump_updated_at: bool,
) -> Result<Vec<AuditChange>, Problem> {
    let mut audits = Vec::new();
    let mut c = Changes::default();
    for (field, value) in changes {
        let Some((_, before)) = stored.iter().find(|(k, _)| k == field) else {
            continue;
        };
        if before.py_str() == value.py_str() {
            continue;
        }
        audits.push(AuditChange {
            key: field.clone(),
            old_value: before.py_str(),
            new_value: value.py_str(),
        });
        if !before.same_as(value) {
            c.set(field, bind_for(value)?);
        }
    }
    if updated_by != Some(changed_by) {
        c.set("updated_by", Bind::Str(changed_by.to_owned()));
    }
    if bump_updated_at {
        c.apply(tx, table, key, key_binds).await?;
    } else if !c.sets.is_empty() {
        let mut sql = format!("UPDATE {table} SET {} WHERE ", c.sets.join(", "));
        let mut n = c.binds.len();
        let mut where_sql = key.to_owned();
        for _ in key_binds {
            n += 1;
            where_sql = where_sql.replacen('?', &format!("${n}"), 1);
        }
        sql.push_str(&where_sql);
        let mut q = sqlx::query(&sql);
        for b in c.binds {
            q = match b {
                Bind::Dec(d) => q.bind(d),
                Bind::Str(s) => q.bind(s),
                Bind::Date(d) => q.bind(d),
                Bind::Int(i) => q.bind(i),
                Bind::Bool(v) => q.bind(v),
                Bind::Time(t) => q.bind(t),
                Bind::OptStr(v) => q.bind(v),
                Bind::OptDec(v) => q.bind(v),
            };
        }
        for k in key_binds {
            q = q.bind(*k);
        }
        q.execute(&mut **tx).await.map_err(db)?;
    }
    Ok(audits)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::unreachable, clippy::panic)]
    use super::*;

    #[test]
    fn routes_are_the_producers() {
        assert_eq!(queue_for("baskfy.swing.scan_now"), "compute");
        assert_eq!(queue_for("baskfy.backtest.run"), "backtest");
        assert_eq!(queue_for("baskfy.twt.scan"), "compute");
        assert_eq!(queue_for("baskfy.vbt.rescan"), "compute");
        assert_eq!(queue_for("baskfy.overlap.catalyst_scan"), "compute");
        assert_eq!(queue_for("baskfy.unknown"), "default");
    }

    #[test]
    fn the_message_is_celerys_protocol_two() {
        let (id, text, queue) = celery_message(
            "baskfy.backtest.run",
            &[OrderedJson::Str("abc".to_owned()), OrderedJson::Bool(true)],
        );
        assert_eq!(queue, "backtest");
        let v = OrderedJson::parse(&text).unwrap();
        let headers = v.get("headers").unwrap();
        assert_eq!(headers.get("id"), Some(&OrderedJson::Str(id.clone())));
        assert_eq!(
            headers.get("argsrepr"),
            Some(&OrderedJson::Str("['abc', True]".to_owned()))
        );
        let Some(OrderedJson::Str(body)) = v.get("body") else {
            unreachable!("body")
        };
        let decoded = base64::engine::general_purpose::STANDARD
            .decode(body)
            .unwrap();
        assert_eq!(
            String::from_utf8(decoded).unwrap(),
            r#"[["abc", true], {}, {"callbacks": null, "errbacks": null, "chain": null, "chord": null}]"#
        );
        assert_eq!(
            binding_member("compute"),
            "compute\u{6}\u{16}\u{6}\u{16}compute"
        );
        assert_eq!(uuid4().len(), 36);
    }
    /// `test_swing_readonly.py` / `test_twt_readonly.py` / `test_overlap_readonly.py`'s source
    /// half, over every write this lane ports: no order verb, no GTT verb, no execution crate.
    #[test]
    fn no_write_here_reaches_a_broker() {
        let sources = [
            include_str!("writes.rs"),
            include_str!("replace.rs"),
            include_str!("pyd.rs"),
            include_str!("../swing/writes.rs"),
            include_str!("../vbt/writes.rs"),
            include_str!("../derivatives/config_writes.rs"),
            include_str!("../overlap/writes.rs"),
            include_str!("../overlap/tags.rs"),
            include_str!("../backtests/writes.rs"),
        ];
        let verbs = [
            ["place", "_order"].concat(),
            ["modify", "_order"].concat(),
            ["cancel", "_order"].concat(),
            ["place", "_gtt"].concat(),
            ["modify", "_gtt"].concat(),
            ["delete", "_gtt"].concat(),
            ["baskfy", "_execution"].concat(),
            ["baskfy", "-execution"].concat(),
        ];
        for source in sources {
            for verb in &verbs {
                assert!(
                    !source.contains(verb.as_str()),
                    "a write module names {verb}"
                );
            }
        }
    }
}
