//! Celery protocol 2 over kombu's Redis transport: envelopes, keys, result metas.

use baskfy_db::OrderedJson;
use chrono::{DateTime, FixedOffset, NaiveDateTime, Offset as _, TimeZone as _, Utc};

use crate::error::WorkerError;
use crate::pyjson::{self, object, s};
use crate::tasks::swing_live::producer::{b64encode, uuid4};

/// kombu's `Channel.sep`.
pub const SEP: &str = "\u{6}\u{16}";
/// kombu's `PRIORITY_STEPS`.
pub const PRIORITY_STEPS: [i64; 4] = [0, 3, 6, 9];
/// kombu's `unacked_key`.
pub const UNACKED: &str = "unacked";
/// kombu's `unacked_index_key`.
pub const UNACKED_INDEX: &str = "unacked_index";
/// kombu's `unacked_mutex_key`.
pub const UNACKED_MUTEX: &str = "unacked_mutex";
/// kombu's `unacked_mutex_expire`, seconds.
pub const UNACKED_MUTEX_EXPIRE_SECS: u64 = 300;
/// `broker_transport_options["visibility_timeout"]`, seconds.
pub const VISIBILITY_TIMEOUT_SECS: f64 = 6.0 * 60.0 * 60.0;
/// `result_expires`, seconds.
pub const RESULT_EXPIRES_SECS: u64 = 7 * 24 * 60 * 60;

/// The app's timezone (`Asia/Kolkata`, no DST).
#[must_use]
pub fn ist() -> FixedOffset {
    FixedOffset::east_opt(5 * 3600 + 30 * 60).unwrap_or_else(|| Utc.fix())
}

/// `datetime.isoformat()` of an aware datetime.
#[must_use]
pub fn py_isoformat(at: DateTime<FixedOffset>) -> String {
    let micros = at.timestamp_subsec_micros();
    let base = at.format("%Y-%m-%dT%H:%M:%S").to_string();
    let offset = at.format("%:z").to_string();
    if micros == 0 {
        format!("{base}{offset}")
    } else {
        format!("{base}.{micros:06}{offset}")
    }
}

/// `Channel.priority(n)`.
#[must_use]
pub fn priority_step(priority: i64) -> i64 {
    PRIORITY_STEPS
        .iter()
        .rev()
        .find(|step| **step <= priority)
        .copied()
        .unwrap_or(0)
}

/// `Channel._q_for_pri(queue, pri)`.
#[must_use]
pub fn queue_key(queue: &str, priority: i64) -> String {
    match priority_step(priority) {
        0 => queue.to_owned(),
        step => format!("{queue}{SEP}{step}"),
    }
}

/// The keys `_brpop_start` blocks on, in its order.
#[must_use]
pub fn brpop_keys(queues: &[String]) -> Vec<String> {
    PRIORITY_STEPS
        .iter()
        .flat_map(|p| queues.iter().map(move |q| queue_key(q, *p)))
        .collect()
}

/// The queue a popped key belongs to (`dest.rsplit(sep, 1)[0]`).
#[must_use]
pub fn queue_of_key(key: &str) -> &str {
    key.rsplit_once(SEP).map_or(key, |(q, _)| q)
}

/// The binding kombu declares for a queue on the default exchange: `(set key, member)`.
#[must_use]
pub fn binding(queue: &str) -> (String, String) {
    (
        format!("_kombu.binding.{queue}"),
        format!("{queue}{SEP}{SEP}{queue}"),
    )
}

/// `repr()` of a JSON-decoded Python value.
#[must_use]
pub fn py_repr(value: &OrderedJson) -> String {
    match value {
        OrderedJson::Null => "None".to_owned(),
        OrderedJson::Bool(true) => "True".to_owned(),
        OrderedJson::Bool(false) => "False".to_owned(),
        OrderedJson::Int(n) => n.to_string(),
        OrderedJson::UInt(n) => n.to_string(),
        OrderedJson::BigInt(digits) => digits.clone(),
        OrderedJson::Float(x) => baskfy_providers::py::repr_float(*x),
        OrderedJson::Str(text) => baskfy_providers::py::repr_str(text),
        OrderedJson::Array(items) => format!(
            "[{}]",
            items.iter().map(py_repr).collect::<Vec<_>>().join(", ")
        ),
        OrderedJson::Object(entries) => format!(
            "{{{}}}",
            entries
                .iter()
                .map(|(k, v)| format!("{}: {}", baskfy_providers::py::repr_str(k), py_repr(v)))
                .collect::<Vec<_>>()
                .join(", ")
        ),
    }
}

/// `base64.b64decode`.
///
/// # Errors
/// Text that is not padded standard base64.
pub fn b64decode(text: &str) -> Result<Vec<u8>, String> {
    let value = |c: u8| -> Result<u32, String> {
        match c {
            b'A'..=b'Z' => Ok(u32::from(c - b'A')),
            b'a'..=b'z' => Ok(u32::from(c - b'a') + 26),
            b'0'..=b'9' => Ok(u32::from(c - b'0') + 52),
            b'+' => Ok(62),
            b'/' => Ok(63),
            _ => Err(format!("invalid base64 byte {c:#x}")),
        }
    };
    let bytes: Vec<u8> = text.bytes().filter(|b| !b.is_ascii_whitespace()).collect();
    if !bytes.len().is_multiple_of(4) {
        return Err("base64 length is not a multiple of 4".to_owned());
    }
    let mut out = Vec::with_capacity(bytes.len() / 4 * 3);
    for chunk in bytes.chunks(4) {
        let pad = chunk.iter().rev().take_while(|b| **b == b'=').count();
        if pad > 2 {
            return Err("bad base64 padding".to_owned());
        }
        let mut n = 0_u32;
        for (i, b) in chunk.iter().enumerate() {
            let v = if i >= 4 - pad { 0 } else { value(*b)? };
            n = (n << 6) | v;
        }
        let [_, b0, b1, b2] = n.to_be_bytes();
        out.push(b0);
        if pad < 2 {
            out.push(b1);
        }
        if pad < 1 {
            out.push(b2);
        }
    }
    Ok(out)
}

/// How `argsrepr` prints positional arguments (`saferepr` of a tuple or a list).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ArgsRepr {
    /// `apply_async(args=())` (Beat).
    Tuple,
    /// A list (`args=[...]`, a retry's `request.args`).
    List,
}

/// A message to publish.
#[derive(Debug, Clone, PartialEq)]
pub struct Outgoing {
    /// `headers.task`.
    pub name: String,
    /// `headers.id`.
    pub task_id: String,
    /// Positional arguments.
    pub args: Vec<OrderedJson>,
    /// How `argsrepr` prints them.
    pub args_repr: ArgsRepr,
    /// Keyword arguments.
    pub kwargs: Vec<(String, OrderedJson)>,
    /// The queue (routing key on the default exchange).
    pub queue: String,
    /// `headers.eta`.
    pub eta: Option<DateTime<FixedOffset>>,
    /// `headers.expires`.
    pub expires: Option<DateTime<FixedOffset>>,
    /// `properties.expiration` (milliseconds).
    pub expiration_ms: Option<u64>,
    /// `headers.retries`.
    pub retries: i64,
    /// `headers.root_id` (`None`: the task's own id).
    pub root_id: Option<String>,
    /// `headers.parent_id`.
    pub parent_id: Option<String>,
    /// `headers.timelimit` (`[hard, soft]`).
    pub timelimit: [OrderedJson; 2],
    /// `properties.priority`.
    pub priority: i64,
    /// `headers.origin`.
    pub origin: String,
    /// `properties.reply_to`.
    pub reply_to: String,
    /// `properties.delivery_tag`.
    pub delivery_tag: String,
}

/// `gen<pid>@<host>` (`anon_nodename()`).
#[must_use]
pub fn origin() -> String {
    format!("gen{}@{}", std::process::id(), hostname())
}

/// The host name Celery's node names carry.
#[must_use]
pub fn hostname() -> String {
    std::env::var("HOSTNAME")
        .ok()
        .filter(|h| !h.is_empty())
        .unwrap_or_else(|| "localhost".to_owned())
}

impl Outgoing {
    /// A fresh message for `name` on `queue` (new task id, reply-to and delivery tag).
    ///
    /// # Errors
    /// The entropy source.
    pub fn new(
        name: &str,
        args: Vec<OrderedJson>,
        args_repr: ArgsRepr,
        kwargs: Vec<(String, OrderedJson)>,
        queue: &str,
    ) -> Result<Self, WorkerError> {
        Ok(Self {
            name: name.to_owned(),
            task_id: uuid4()?,
            args,
            args_repr,
            kwargs,
            queue: queue.to_owned(),
            eta: None,
            expires: None,
            expiration_ms: None,
            retries: 0,
            root_id: None,
            parent_id: None,
            timelimit: [OrderedJson::Null, OrderedJson::Null],
            priority: 0,
            origin: origin(),
            reply_to: uuid4()?,
            delivery_tag: uuid4()?,
        })
    }

    fn argsrepr(&self) -> String {
        let parts: Vec<String> = self.args.iter().map(py_repr).collect();
        match (self.args_repr, parts.len()) {
            (ArgsRepr::List, _) => format!("[{}]", parts.join(", ")),
            (ArgsRepr::Tuple, 1) => format!("({},)", parts.join("")),
            (ArgsRepr::Tuple, _) => format!("({})", parts.join(", ")),
        }
    }

    /// The JSON text kombu pushes.
    ///
    /// # Errors
    /// A value JSON cannot carry.
    pub fn encode(&self) -> Result<String, String> {
        let body = pyjson::dumps(&OrderedJson::Array(vec![
            OrderedJson::Array(self.args.clone()),
            OrderedJson::Object(self.kwargs.clone()),
            object([
                ("callbacks", OrderedJson::Null),
                ("errbacks", OrderedJson::Null),
                ("chain", OrderedJson::Null),
                ("chord", OrderedJson::Null),
            ]),
        ]))?;
        let when = |at: Option<DateTime<FixedOffset>>| at.map_or(OrderedJson::Null, |t| s(py_isoformat(t)));
        let root = self.root_id.clone().unwrap_or_else(|| self.task_id.clone());
        let mut properties = vec![
            ("correlation_id".to_owned(), s(&self.task_id)),
            ("reply_to".to_owned(), s(&self.reply_to)),
            ("delivery_mode".to_owned(), OrderedJson::Int(2)),
        ];
        if let Some(ms) = self.expiration_ms {
            properties.push(("expiration".to_owned(), s(ms.to_string())));
        }
        properties.extend([
            (
                "delivery_info".to_owned(),
                object([("exchange", s("")), ("routing_key", s(&self.queue))]),
            ),
            ("priority".to_owned(), OrderedJson::Int(self.priority)),
            ("body_encoding".to_owned(), s("base64")),
            ("delivery_tag".to_owned(), s(&self.delivery_tag)),
        ]);
        let envelope = object([
            ("body", s(b64encode(body.as_bytes()))),
            ("content-encoding", s("utf-8")),
            ("content-type", s("application/json")),
            (
                "headers",
                object([
                    ("lang", s("py")),
                    ("task", s(&self.name)),
                    ("id", s(&self.task_id)),
                    ("shadow", OrderedJson::Null),
                    ("eta", when(self.eta)),
                    ("expires", when(self.expires)),
                    ("group", OrderedJson::Null),
                    ("group_index", OrderedJson::Null),
                    ("retries", OrderedJson::Int(self.retries)),
                    ("timelimit", OrderedJson::Array(self.timelimit.to_vec())),
                    ("root_id", s(root)),
                    ("parent_id", pyjson::opt_s(self.parent_id.clone())),
                    ("argsrepr", s(self.argsrepr())),
                    (
                        "kwargsrepr",
                        s(py_repr(&OrderedJson::Object(self.kwargs.clone()))),
                    ),
                    ("origin", s(&self.origin)),
                    ("ignore_result", OrderedJson::Bool(false)),
                    ("replaced_task_nesting", OrderedJson::Int(0)),
                    ("stamped_headers", OrderedJson::Null),
                    ("stamps", OrderedJson::Object(Vec::new())),
                ]),
            ),
            ("properties", OrderedJson::Object(properties)),
        ]);
        pyjson::dumps(&envelope)
    }
}

/// A delivered message, decoded.
#[derive(Debug, Clone, PartialEq)]
pub struct Incoming {
    /// The envelope as delivered.
    pub envelope: OrderedJson,
    /// `headers.task`.
    pub task: String,
    /// `headers.id`.
    pub id: String,
    /// Positional arguments.
    pub args: Vec<OrderedJson>,
    /// Keyword arguments.
    pub kwargs: Vec<(String, OrderedJson)>,
    /// `headers.retries`.
    pub retries: i64,
    /// `headers.eta`.
    pub eta: Option<DateTime<FixedOffset>>,
    /// `headers.expires`.
    pub expires: Option<DateTime<FixedOffset>>,
    /// `headers.timelimit` as delivered.
    pub timelimit: [OrderedJson; 2],
    /// `headers.root_id`.
    pub root_id: Option<String>,
    /// `headers.parent_id`.
    pub parent_id: Option<String>,
    /// `headers.ignore_result`.
    pub ignore_result: bool,
    /// `properties.delivery_tag`.
    pub delivery_tag: String,
    /// `delivery_info.exchange`.
    pub exchange: String,
    /// `delivery_info.routing_key`.
    pub routing_key: String,
    /// `properties.priority`.
    pub priority: i64,
    /// `delivery_info.redelivered` (set by kombu's restore).
    pub redelivered: bool,
    /// `headers.baskfy_redeliveries`.
    pub redeliveries: i64,
}

fn text_of(value: Option<&OrderedJson>) -> Option<String> {
    match value {
        Some(OrderedJson::Str(t)) => Some(t.clone()),
        _ => None,
    }
}

fn int_of(value: Option<&OrderedJson>) -> Option<i64> {
    match value {
        Some(OrderedJson::Int(n)) => Some(*n),
        Some(OrderedJson::Str(t)) => t.parse().ok(),
        _ => None,
    }
}

/// An ISO datetime from a header (naive means the app's timezone).
fn when_of(value: Option<&OrderedJson>) -> Result<Option<DateTime<FixedOffset>>, String> {
    let Some(text) = text_of(value) else {
        return Ok(None);
    };
    if let Ok(at) = DateTime::parse_from_rfc3339(&text) {
        return Ok(Some(at));
    }
    NaiveDateTime::parse_from_str(&text, "%Y-%m-%dT%H:%M:%S%.f")
        .ok()
        .and_then(|naive| ist().from_local_datetime(&naive).single())
        .map(Some)
        .ok_or_else(|| format!("unparseable datetime {text:?}"))
}

/// Decode a message kombu delivered.
///
/// # Errors
/// Not a protocol-2 JSON task message.
pub fn decode(text: &str) -> Result<Incoming, String> {
    let envelope = OrderedJson::parse(text).map_err(|e| format!("not JSON: {e}"))?;
    let headers = envelope.get("headers").ok_or("no headers")?;
    let properties = envelope.get("properties").ok_or("no properties")?;
    let content_type = text_of(envelope.get("content-type")).unwrap_or_default();
    if content_type != "application/json" {
        return Err(format!("content type {content_type:?} is not accepted"));
    }
    let task = text_of(headers.get("task")).ok_or("no headers.task (protocol 1?)")?;
    let id = text_of(headers.get("id")).ok_or("no headers.id")?;
    let raw_body = text_of(envelope.get("body")).ok_or("no body")?;
    let body = match text_of(properties.get("body_encoding")).as_deref() {
        Some("base64") => String::from_utf8(b64decode(&raw_body)?)
            .map_err(|e| format!("body is not UTF-8: {e}"))?,
        _ => raw_body,
    };
    let (args, kwargs) = match OrderedJson::parse(&body).map_err(|e| format!("body: {e}"))? {
        OrderedJson::Array(parts) => match parts.as_slice() {
            [OrderedJson::Array(a), OrderedJson::Object(k), ..] => (a.clone(), k.clone()),
            _ => return Err("body is not [args, kwargs, embed]".to_owned()),
        },
        _ => return Err("body is not [args, kwargs, embed]".to_owned()),
    };
    let info = properties.get("delivery_info");
    let timelimit = match headers.get("timelimit") {
        Some(OrderedJson::Array(v)) => [
            v.first().cloned().unwrap_or(OrderedJson::Null),
            v.get(1).cloned().unwrap_or(OrderedJson::Null),
        ],
        _ => [OrderedJson::Null, OrderedJson::Null],
    };
    let flag = |v: Option<&OrderedJson>| matches!(v, Some(OrderedJson::Bool(true)));
    Ok(Incoming {
        task,
        id,
        args,
        kwargs,
        retries: int_of(headers.get("retries")).unwrap_or(0),
        eta: when_of(headers.get("eta"))?,
        expires: when_of(headers.get("expires"))?,
        timelimit,
        root_id: text_of(headers.get("root_id")),
        parent_id: text_of(headers.get("parent_id")),
        ignore_result: flag(headers.get("ignore_result")),
        delivery_tag: text_of(properties.get("delivery_tag")).ok_or("no delivery_tag")?,
        exchange: text_of(info.and_then(|i| i.get("exchange"))).unwrap_or_default(),
        routing_key: text_of(info.and_then(|i| i.get("routing_key"))).unwrap_or_default(),
        priority: int_of(properties.get("priority")).unwrap_or(0).clamp(0, 9),
        redelivered: flag(info.and_then(|i| i.get("redelivered")))
            || flag(headers.get("redelivered")),
        redeliveries: int_of(headers.get("baskfy_redeliveries")).unwrap_or(0),
        envelope,
    })
}

impl Incoming {
    /// `headers.timelimit[0]` in seconds, if any.
    #[must_use]
    pub fn hard_time_limit(&self) -> Option<f64> {
        match &self.timelimit[0] {
            OrderedJson::Int(n) => Some(*n as f64),
            OrderedJson::UInt(n) => Some(*n as f64),
            OrderedJson::Float(x) => Some(*x),
            _ => None,
        }
    }

    /// `task.retry(countdown=...)`'s message: same id and arguments, one more retry, an eta.
    ///
    /// # Errors
    /// The entropy source.
    pub fn retry_message(&self, eta: DateTime<FixedOffset>) -> Result<Outgoing, WorkerError> {
        let queue = if self.routing_key.is_empty() {
            crate::tasks::route_by_name(&self.task).name().to_owned()
        } else {
            self.routing_key.clone()
        };
        let mut out = Outgoing::new(
            &self.task,
            self.args.clone(),
            ArgsRepr::List,
            self.kwargs.clone(),
            &queue,
        )?;
        out.task_id.clone_from(&self.id);
        out.retries = self.retries + 1;
        out.eta = Some(eta);
        out.expires = self.expires;
        out.root_id = Some(self.root_id.clone().unwrap_or_else(|| self.id.clone()));
        out.parent_id.clone_from(&self.parent_id);
        out.timelimit = self.timelimit.clone();
        out.priority = self.priority;
        Ok(out)
    }
}

/// kombu's `_do_restore_message` payload change: mark the envelope redelivered.
#[must_use]
pub fn mark_redelivered(mut envelope: OrderedJson) -> OrderedJson {
    fn set(entries: &mut Vec<(String, OrderedJson)>, key: &str) {
        pyjson::update(entries, key, OrderedJson::Bool(true));
    }
    if let OrderedJson::Object(top) = &mut envelope {
        let has_both = top.iter().any(|(k, _)| k == "headers")
            && top.iter().any(|(k, v)| {
                k == "properties" && v.get("delivery_info").is_some()
            });
        if !has_both {
            return envelope;
        }
        for (key, value) in top.iter_mut() {
            match (key.as_str(), value) {
                ("headers", OrderedJson::Object(h)) => set(h, "redelivered"),
                ("properties", OrderedJson::Object(p)) => {
                    for (pk, pv) in p.iter_mut() {
                        if let ("delivery_info", OrderedJson::Object(d)) = (pk.as_str(), pv) {
                            set(d, "redelivered");
                        }
                    }
                }
                _ => {}
            }
        }
    }
    envelope
}

/// The module Celery names for an exception class (`exc_module`).
#[must_use]
pub fn exc_module(type_name: &str) -> &'static str {
    const BUILTINS: &[&str] = &[
        "Exception",
        "ValueError",
        "KeyError",
        "TypeError",
        "RuntimeError",
        "LookupError",
        "IndexError",
        "AttributeError",
        "OSError",
        "FileNotFoundError",
        "PermissionError",
        "TimeoutError",
        "ConnectionError",
        "NotImplementedError",
        "ZeroDivisionError",
        "AssertionError",
        "ArithmeticError",
    ];
    match type_name {
        "NotRegistered" | "TaskRevokedError" => "celery.exceptions",
        "TimeLimitExceeded" => "billiard.exceptions",
        t if BUILTINS.contains(&t) => "builtins",
        _ => "baskfy_worker",
    }
}

/// The `result` of a failure meta (`exception_to_python` inverse).
#[must_use]
pub fn exc_result(type_name: &str, exc_message: Vec<OrderedJson>) -> OrderedJson {
    object([
        ("exc_type", s(type_name)),
        ("exc_message", OrderedJson::Array(exc_message)),
        ("exc_module", s(exc_module(type_name))),
    ])
}

/// `celery-task-meta-<id>`.
#[must_use]
pub fn meta_key(task_id: &str) -> String {
    format!("celery-task-meta-{task_id}")
}

/// The Redis backend's stored meta.
///
/// # Errors
/// A value JSON cannot carry.
pub fn meta(
    task_id: &str,
    status: &str,
    result: OrderedJson,
    traceback: Option<&str>,
    children: OrderedJson,
    date_done: Option<DateTime<FixedOffset>>,
) -> Result<String, String> {
    pyjson::dumps(&object([
        ("status", s(status)),
        ("result", result),
        ("traceback", pyjson::opt_s(traceback)),
        ("children", children),
        (
            "date_done",
            date_done.map_or(OrderedJson::Null, |d| s(py_isoformat(d))),
        ),
        ("task_id", s(task_id)),
    ]))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    fn fixtures() -> OrderedJson {
        OrderedJson::parse(include_str!("../../tests/fixtures/celery_messages.json")).unwrap()
    }

    fn at(text: &str) -> DateTime<FixedOffset> {
        DateTime::parse_from_rfc3339(text).unwrap()
    }

    fn outgoing_like(captured: &OrderedJson) -> Outgoing {
        let m = decode(&pyjson::dumps(captured).unwrap()).unwrap();
        let headers = captured.get("headers").unwrap();
        let props = captured.get("properties").unwrap();
        let text = |v: Option<&OrderedJson>| text_of(v).unwrap();
        let args_repr = if text(headers.get("argsrepr")).starts_with('(') {
            ArgsRepr::Tuple
        } else {
            ArgsRepr::List
        };
        Outgoing {
            name: m.task.clone(),
            task_id: m.id.clone(),
            args: m.args.clone(),
            args_repr,
            kwargs: m.kwargs.clone(),
            queue: m.routing_key.clone(),
            eta: m.eta,
            expires: m.expires,
            expiration_ms: text_of(props.get("expiration")).map(|t| t.parse().unwrap()),
            retries: m.retries,
            root_id: m.root_id.clone(),
            parent_id: m.parent_id.clone(),
            timelimit: m.timelimit.clone(),
            priority: m.priority,
            origin: text(headers.get("origin")),
            reply_to: text(props.get("reply_to")),
            delivery_tag: m.delivery_tag.clone(),
        }
    }

    #[test]
    fn every_captured_message_round_trips_through_encode() {
        let all = fixtures();
        for key in [
            "send_task_kwargs",
            "beat_style_expires_countdown",
            "send_task_args",
            "task_time_limit",
            "beat_style_kwargs",
        ] {
            let captured = all.get(key).unwrap();
            let encoded = outgoing_like(captured).encode().unwrap();
            assert_eq!(&OrderedJson::parse(&encoded).unwrap(), captured, "{key}");
        }
    }

    #[test]
    fn decode_reads_pythons_messages() {
        let all = fixtures();
        let text = pyjson::dumps(all.get("send_task_kwargs").unwrap()).unwrap();
        let m = decode(&text).unwrap();
        assert_eq!(m.task, "baskfy.pipeline.nightly");
        assert_eq!(m.id, "11111111-1111-4111-8111-111111111111");
        assert!(m.args.is_empty());
        assert_eq!(m.kwargs, vec![("trade_date".to_owned(), s("2026-10-30"))]);
        assert_eq!(m.routing_key, "default");
        assert!(m.eta.is_none() && !m.redelivered);

        let captured = all.get("beat_style_expires_countdown").unwrap();
        let beat = decode(&pyjson::dumps(captured).unwrap()).unwrap();
        let headers = captured.get("headers").unwrap();
        assert_eq!(
            Some(s(py_isoformat(beat.eta.unwrap()))).as_ref(),
            headers.get("eta")
        );
        assert_eq!(
            Some(s(py_isoformat(beat.expires.unwrap()))).as_ref(),
            headers.get("expires")
        );
        assert_eq!(
            beat.expires.unwrap() - beat.eta.unwrap(),
            chrono::Duration::seconds(35)
        );

        let args = decode(&pyjson::dumps(all.get("send_task_args").unwrap()).unwrap()).unwrap();
        assert_eq!(args.args, vec![OrderedJson::Int(7), s("x")]);
        assert_eq!(args.routing_key, "compute");

        let limited = decode(&pyjson::dumps(all.get("task_time_limit").unwrap()).unwrap()).unwrap();
        assert_eq!(limited.hard_time_limit(), Some(1500.0));
    }

    #[test]
    fn a_restored_message_is_marked_redelivered() {
        let all = fixtures();
        let marked = mark_redelivered(all.get("send_task_args").unwrap().clone());
        let m = decode(&pyjson::dumps(&marked).unwrap()).unwrap();
        assert!(m.redelivered);
    }

    #[test]
    fn the_retry_message_keeps_the_id_and_counts_the_retry() {
        let all = fixtures();
        let m = decode(&pyjson::dumps(all.get("send_task_args").unwrap()).unwrap()).unwrap();
        let eta = at("2026-10-31T16:44:00+05:30");
        let retry = m.retry_message(eta).unwrap();
        let text = retry.encode().unwrap();
        let back = decode(&text).unwrap();
        assert_eq!(back.id, m.id);
        assert_eq!(back.retries, 1);
        assert_eq!(back.eta, Some(eta));
        assert_eq!(back.routing_key, "compute");
        assert_eq!(back.args, m.args);
        let v = OrderedJson::parse(&text).unwrap();
        assert_eq!(
            v.get("headers").unwrap().get("argsrepr"),
            Some(&s("[7, 'x']"))
        );
        assert_eq!(
            v.get("headers").unwrap().get("eta"),
            Some(&s("2026-10-31T16:44:00+05:30"))
        );
    }

    #[test]
    fn metas_match_the_redis_backend() {
        let all = fixtures();
        let cases = [
            ("meta_success", "SUCCESS"),
            ("meta_failure", "FAILURE"),
            ("meta_started", "STARTED"),
            ("meta_retry", "RETRY"),
            ("meta_revoked", "REVOKED"),
            ("meta_timelimit", "FAILURE"),
            ("meta_not_registered", "FAILURE"),
        ];
        for (key, status) in cases {
            let want = all.get(key).unwrap();
            let id = text_of(want.get("task_id")).unwrap();
            let done = text_of(want.get("date_done")).map(|t| at(&t));
            let result = match want.get("result").unwrap() {
                r @ OrderedJson::Object(e) if e.iter().any(|(k, _)| k == "exc_type") => {
                    let ty = text_of(r.get("exc_type")).unwrap();
                    let OrderedJson::Array(msg) = r.get("exc_message").unwrap().clone() else {
                        unreachable!()
                    };
                    exc_result(&ty, msg)
                }
                other => other.clone(),
            };
            let tb = text_of(want.get("traceback"));
            let got = meta(&id, status, result, tb.as_deref(), OrderedJson::Null, done).unwrap();
            assert_eq!(&OrderedJson::parse(&got).unwrap(), want, "{key}");
        }
    }

    #[test]
    fn keys_and_priorities_are_kombus() {
        assert_eq!(queue_key("default", 0), "default");
        assert_eq!(queue_key("default", 5), "default\u{6}\u{16}3");
        assert_eq!(queue_key("default", 9), "default\u{6}\u{16}9");
        assert_eq!(queue_of_key("compute\u{6}\u{16}6"), "compute");
        assert_eq!(
            brpop_keys(&["a".to_owned(), "b".to_owned()]),
            vec![
                "a",
                "b",
                "a\u{6}\u{16}3",
                "b\u{6}\u{16}3",
                "a\u{6}\u{16}6",
                "b\u{6}\u{16}6",
                "a\u{6}\u{16}9",
                "b\u{6}\u{16}9"
            ]
        );
        let all = fixtures();
        let OrderedJson::Array(b) = all.get("bindings").unwrap() else {
            unreachable!()
        };
        assert_eq!(b, &vec![s(binding("default").1)]);
    }

    #[test]
    fn base64_round_trips() {
        for sample in [&b""[..], b"f", b"fo", b"foo", b"[[118], {}]"] {
            assert_eq!(b64decode(&b64encode(sample)).unwrap(), sample);
        }
    }
}
