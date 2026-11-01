//! `twt_scan_task.apply_async(args=[run_id], queue="compute")` (the TW12 publisher) and
//! `vbt_rescan_task.apply_async(...)` (the VB12 sweep), shared by both lanes. Kombu's Redis
//! transport, as the API's producer writes it
//! (`baskfy_api` `routes::sleeves::writes::celery_message`, crate-private there): the message
//! JSON (protocol 2, the body base64), `LPUSH`ed on the queue's list after the queue's binding
//! is `SADD`ed. Routing is the call's `queue=compute`, which for both tasks is also its route.
//!
//! The task id is a fresh uuid4 taken from Postgres (`gen_random_uuid()`), so the lane needs no
//! new dependency. **Law 2:** the message is a scan request; a scan places nothing.

use baskfy_db::OrderedJson;
use sqlx::PgConnection;

use crate::error::WorkerError;
use crate::pyjson::{self, object, s};

/// The queue the scan and the rescan are published to (`QUEUE_COMPUTE`).
pub(crate) const QUEUE_COMPUTE: &str = "compute";

/// RFC 4648 base64 with padding (`base64.b64encode`).
pub(crate) fn b64encode(bytes: &[u8]) -> String {
    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let b0 = chunk.first().copied().unwrap_or(0);
        let b1 = chunk.get(1).copied().unwrap_or(0);
        let b2 = chunk.get(2).copied().unwrap_or(0);
        let n = (u32::from(b0) << 16) | (u32::from(b1) << 8) | u32::from(b2);
        let pick = |shift: u32| char::from(TABLE[usize::try_from((n >> shift) & 63).unwrap_or(0)]);
        out.push(pick(18));
        out.push(pick(12));
        out.push(if chunk.len() > 1 { pick(6) } else { '=' });
        out.push(if chunk.len() > 2 { pick(0) } else { '=' });
    }
    out
}

/// `gen<pid>@<host>` — Celery's `origin`.
fn origin() -> String {
    let host = std::env::var("HOSTNAME")
        .ok()
        .filter(|h| !h.is_empty())
        .unwrap_or_else(|| "localhost".to_owned());
    format!("gen{}@{host}", std::process::id())
}

/// The kombu envelope for `name(*args)` on `queue`, with its task id.
pub(crate) fn message(
    name: &str,
    args: &[i64],
    queue: &str,
    task_id: &str,
    reply_to: &str,
    delivery_tag: &str,
) -> Result<String, WorkerError> {
    let to_json =
        |v: &OrderedJson| pyjson::dumps(v).map_err(|e| WorkerError::python("ValueError", e));
    let arg_values: Vec<OrderedJson> = args.iter().map(|a| OrderedJson::Int(*a)).collect();
    let body = to_json(&OrderedJson::Array(vec![
        OrderedJson::Array(arg_values),
        OrderedJson::Object(Vec::new()),
        object([
            ("callbacks", OrderedJson::Null),
            ("errbacks", OrderedJson::Null),
            ("chain", OrderedJson::Null),
            ("chord", OrderedJson::Null),
        ]),
    ]))?;
    let argsrepr = format!(
        "[{}]",
        args.iter()
            .map(i64::to_string)
            .collect::<Vec<_>>()
            .join(", ")
    );
    let envelope = object([
        ("body", s(b64encode(body.as_bytes()))),
        ("content-encoding", s("utf-8")),
        ("content-type", s("application/json")),
        (
            "headers",
            object([
                ("lang", s("py")),
                ("task", s(name)),
                ("id", s(task_id)),
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
                ("root_id", s(task_id)),
                ("parent_id", OrderedJson::Null),
                ("argsrepr", s(argsrepr)),
                ("kwargsrepr", s("{}")),
                ("origin", s(origin())),
                ("ignore_result", OrderedJson::Bool(false)),
                ("replaced_task_nesting", OrderedJson::Int(0)),
                ("stamped_headers", OrderedJson::Null),
                ("stamps", OrderedJson::Object(Vec::new())),
            ]),
        ),
        (
            "properties",
            object([
                ("correlation_id", s(task_id)),
                ("reply_to", s(reply_to)),
                ("delivery_mode", OrderedJson::Int(2)),
                (
                    "delivery_info",
                    object([("exchange", s("")), ("routing_key", s(queue))]),
                ),
                ("priority", OrderedJson::Int(0)),
                ("body_encoding", s("base64")),
                ("delivery_tag", s(delivery_tag)),
            ]),
        ),
    ]);
    to_json(&envelope)
}

async fn uuid4(conn: &mut PgConnection) -> Result<String, WorkerError> {
    let (id,): (String,) = sqlx::query_as("SELECT gen_random_uuid()::text")
        .fetch_one(&mut *conn)
        .await?;
    Ok(id)
}

/// A connection to the broker (`app.broker_connection()`), opened once per publisher pass.
pub(crate) async fn broker(url: &str) -> Result<redis::aio::MultiplexedConnection, WorkerError> {
    let client = redis::Client::open(url)
        .map_err(|e| WorkerError::python("OperationalError", e.to_string()))?;
    client
        .get_multiplexed_async_connection()
        .await
        .map_err(|e| WorkerError::python("OperationalError", e.to_string()))
}

/// `task.apply_async(args=[arg], queue=queue)`: publish and answer the task id.
pub(crate) async fn apply_async(
    conn: &mut PgConnection,
    redis: &mut redis::aio::MultiplexedConnection,
    name: &str,
    arg: i64,
    queue: &str,
) -> Result<String, WorkerError> {
    let task_id = uuid4(conn).await?;
    let reply_to = uuid4(conn).await?;
    let delivery_tag = uuid4(conn).await?;
    let text = message(name, &[arg], queue, &task_id, &reply_to, &delivery_tag)?;
    let binding = format!("{queue}\u{6}\u{16}\u{6}\u{16}{queue}");
    redis::pipe()
        .cmd("SADD")
        .arg(format!("_kombu.binding.{queue}"))
        .arg(binding)
        .ignore()
        .cmd("LPUSH")
        .arg(queue)
        .arg(text)
        .ignore()
        .query_async::<()>(redis)
        .await
        .map_err(|e| WorkerError::python("OperationalError", e.to_string()))?;
    Ok(task_id)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn base64_is_pythons() {
        assert_eq!(b64encode(b""), "");
        assert_eq!(b64encode(b"f"), "Zg==");
        assert_eq!(b64encode(b"fo"), "Zm8=");
        assert_eq!(b64encode(b"foo"), "Zm9v");
        assert_eq!(b64encode(b"[[118], {}]"), "W1sxMThdLCB7fV0=");
    }

    #[test]
    fn the_envelope_routes_to_compute_and_carries_the_run_id() {
        let text =
            message("baskfy.twt.scan", &[118], QUEUE_COMPUTE, "t", "r", "d").unwrap_or_default();
        let v: serde_json::Value = serde_json::from_str(&text).unwrap_or_default();
        assert_eq!(v["headers"]["task"], "baskfy.twt.scan");
        let vbt =
            message("baskfy.vbt.rescan", &[7], QUEUE_COMPUTE, "t", "r", "d").unwrap_or_default();
        let w: serde_json::Value = serde_json::from_str(&vbt).unwrap_or_default();
        assert_eq!(w["headers"]["task"], "baskfy.vbt.rescan");
        assert_eq!(w["properties"]["delivery_info"]["routing_key"], "compute");
        assert_eq!(v["headers"]["argsrepr"], "[118]");
        assert_eq!(v["properties"]["delivery_info"]["routing_key"], "compute");
        assert_eq!(
            v["body"],
            b64encode(
                b"[[118], {}, {\"callbacks\": null, \"errbacks\": null, \"chain\": null, \"chord\": null}]"
            )
        );
    }
}
