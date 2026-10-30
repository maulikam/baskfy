//! The worker as a **Celery producer**: `task.apply_async(args=[...], queue=...)` from inside a
//! task body (the swing sweep publishes `baskfy.swing.scan_now`), over kombu's Redis transport.
//!
//! kombu declares the queue's binding (`SADD _kombu.binding.<queue>`) and `LPUSH`es the message
//! — protocol 2, JSON, the body base64-encoded — onto the list named after the queue. The
//! envelope is the one Celery 5 builds for a message published outside a running worker request
//! (`parent_id` null, `root_id` the task's own id). The task id is a fresh UUID4; `origin` is
//! `gen<pid>@<host>`. The harness normalises ids, `origin` and the delivery tag (R8.2).
//!
//! Public so a sibling lane with the same need (the VBT and TWT sweeps) can reuse it rather than
//! write a second copy. **Law 2:** these are the worker's own queues (scans), never an order.

use baskfy_db::OrderedJson;
use redis::AsyncCommands as _;

use crate::error::WorkerError;
use crate::pyjson;

/// A random UUID4, Python's `str(uuid.uuid4())`.
///
/// # Errors
/// The OS entropy source could not be read.
pub fn uuid4() -> Result<String, WorkerError> {
    use std::io::Read as _;
    let mut b = [0_u8; 16];
    let mut file = std::fs::File::open("/dev/urandom")
        .map_err(|e| WorkerError::python("OSError", e.to_string()))?;
    file.read_exact(&mut b)
        .map_err(|e| WorkerError::python("OSError", e.to_string()))?;
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    let hex: String = b.iter().map(|x| format!("{x:02x}")).collect();
    Ok(format!(
        "{}-{}-{}-{}-{}",
        &hex[0..8],
        &hex[8..12],
        &hex[12..16],
        &hex[16..20],
        &hex[20..32]
    ))
}

const B64: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/// `base64.b64encode(data)` (standard alphabet, padded).
#[must_use]
pub fn b64encode(data: &[u8]) -> String {
    let mut out = String::with_capacity(data.len().div_ceil(3) * 4);
    for chunk in data.chunks(3) {
        let b0 = chunk.first().copied().unwrap_or(0);
        let b1 = chunk.get(1).copied().unwrap_or(0);
        let b2 = chunk.get(2).copied().unwrap_or(0);
        let n = (u32::from(b0) << 16) | (u32::from(b1) << 8) | u32::from(b2);
        let pick = |shift: u32| char::from(B64[((n >> shift) & 0x3f) as usize]);
        out.push(pick(18));
        out.push(pick(12));
        out.push(if chunk.len() > 1 { pick(6) } else { '=' });
        out.push(if chunk.len() > 2 { pick(0) } else { '=' });
    }
    out
}

/// `gen<pid>@<host>` — Celery's `origin` (`anon_nodename()`).
fn origin() -> String {
    let host = std::env::var("HOSTNAME")
        .ok()
        .filter(|h| !h.is_empty())
        .unwrap_or_else(|| "localhost".to_owned());
    format!("gen{}@{host}", std::process::id())
}

/// `repr(list(args))` for JSON scalars (`[42]`).
fn args_repr(args: &[OrderedJson]) -> Result<String, WorkerError> {
    let parts: Result<Vec<String>, WorkerError> = args
        .iter()
        .map(|a| match a {
            OrderedJson::Null => Ok("None".to_owned()),
            OrderedJson::Bool(true) => Ok("True".to_owned()),
            OrderedJson::Bool(false) => Ok("False".to_owned()),
            OrderedJson::Str(s) => Ok(baskfy_providers::py::repr_str(s)),
            other => pyjson::dumps(other).map_err(|e| WorkerError::python("ValueError", e)),
        })
        .collect();
    Ok(format!("[{}]", parts?.join(", ")))
}

/// The message `task.apply_async(args=args, queue=queue)` publishes, and its task id.
///
/// # Errors
/// The entropy source, or a value JSON cannot carry.
pub fn celery_message(
    name: &str,
    args: &[OrderedJson],
    queue: &str,
) -> Result<(String, String), WorkerError> {
    let task_id = uuid4()?;
    let body = pyjson::dumps(&OrderedJson::Array(vec![
        OrderedJson::Array(args.to_vec()),
        OrderedJson::Object(Vec::new()),
        pyjson::object([
            ("callbacks", OrderedJson::Null),
            ("errbacks", OrderedJson::Null),
            ("chain", OrderedJson::Null),
            ("chord", OrderedJson::Null),
        ]),
    ]))
    .map_err(|e| WorkerError::python("ValueError", e))?;
    let s = |text: &str| pyjson::s(text);
    let message = pyjson::object([
        ("body", s(&b64encode(body.as_bytes()))),
        ("content-encoding", s("utf-8")),
        ("content-type", s("application/json")),
        (
            "headers",
            pyjson::object([
                ("lang", s("py")),
                ("task", s(name)),
                ("id", s(&task_id)),
                ("shadow", OrderedJson::Null),
                ("eta", OrderedJson::Null),
                ("expires", OrderedJson::Null),
                ("group", OrderedJson::Null),
                ("group_index", OrderedJson::Null),
                ("retries", OrderedJson::Int(0)),
                (
                    "timelimit",
                    OrderedJson::Array(vec![OrderedJson::Null, OrderedJson::Null]),
                ),
                ("root_id", s(&task_id)),
                ("parent_id", OrderedJson::Null),
                ("argsrepr", s(&args_repr(args)?)),
                ("kwargsrepr", s("{}")),
                ("origin", s(&origin())),
                ("ignore_result", OrderedJson::Bool(false)),
                ("replaced_task_nesting", OrderedJson::Int(0)),
                ("stamped_headers", OrderedJson::Null),
                ("stamps", OrderedJson::Object(Vec::new())),
            ]),
        ),
        (
            "properties",
            pyjson::object([
                ("correlation_id", s(&task_id)),
                ("reply_to", s(&uuid4()?)),
                ("delivery_mode", OrderedJson::Int(2)),
                (
                    "delivery_info",
                    pyjson::object([("exchange", s("")), ("routing_key", s(queue))]),
                ),
                ("priority", OrderedJson::Int(0)),
                ("body_encoding", s("base64")),
                ("delivery_tag", s(&uuid4()?)),
            ]),
        ),
    ]);
    let text = pyjson::dumps(&message).map_err(|e| WorkerError::python("ValueError", e))?;
    Ok((task_id, text))
}

/// `task.apply_async(args=args, queue=queue).id` against the broker at `broker_url`.
///
/// # Errors
/// `OperationalError` (kombu's) when the broker cannot be reached or refuses the write.
pub async fn apply_async(
    broker_url: &str,
    name: &str,
    args: &[OrderedJson],
    queue: &str,
) -> Result<String, WorkerError> {
    let (task_id, message) = celery_message(name, args, queue)?;
    let broker = |e: redis::RedisError| WorkerError::python("OperationalError", e.to_string());
    let client = redis::Client::open(broker_url).map_err(broker)?;
    let mut conn = client
        .get_multiplexed_async_connection()
        .await
        .map_err(broker)?;
    let member = format!("{queue}\u{6}\u{16}\u{6}\u{16}{queue}");
    let _: i64 = conn
        .sadd(format!("_kombu.binding.{queue}"), member)
        .await
        .map_err(broker)?;
    let _: i64 = conn.lpush(queue, message).await.map_err(broker)?;
    Ok(task_id)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn base64_matches_python() {
        assert_eq!(b64encode(b""), "");
        assert_eq!(b64encode(b"f"), "Zg==");
        assert_eq!(b64encode(b"fo"), "Zm8=");
        assert_eq!(b64encode(b"foo"), "Zm9v");
        assert_eq!(b64encode(b"[[42], {}]"), "W1s0Ml0sIHt9XQ==");
    }

    #[test]
    fn a_uuid4_has_its_version_and_variant() {
        let id = uuid4().unwrap_or_default();
        assert_eq!(id.len(), 36);
        assert_eq!(&id[14..15], "4");
        assert!(matches!(&id[19..20], "8" | "9" | "a" | "b"));
    }

    #[test]
    fn the_args_repr_is_pythons() {
        assert_eq!(
            args_repr(&[OrderedJson::Int(42)]).unwrap_or_default(),
            "[42]"
        );
    }
}
