//! The billing **writes** of `baskfy_api.routers.billing`, with `baskfy_api.billing` and the thin
//! Razorpay client of `baskfy_api.razorpay`:
//!
//! * `POST /checkout/session` — a Razorpay order (Forever) or subscription (monthly/yearly) for a
//!   plan on sale, and the pending `subscription` row (`past_due`) that holds its id;
//! * `POST /webhooks/razorpay` — signature-verified (HMAC-SHA256 of the raw body with the webhook
//!   secret, compared in constant time), idempotent by event id (`ON CONFLICT DO NOTHING` on
//!   `webhook_event`), driving the subscription lifecycle.
//!
//! The gateway is reached only through `baskfy_providers::net::GuardedClient`; on the bed its
//! base URL is a closed local port, so a checkout that gets as far as the gateway is refused by
//! the connection (503) on both sides and no call leaves the box.
//!
//! A captured payment (`payment.captured`, `order.paid`, `subscription.charged` carrying a
//! payment entity) that attributes to an account and a plan is recorded as Python records it: the
//! `payment` row, then the GST tax invoice ([`super::invoice`]) — the gapless number from
//! `invoice_counter`, the inclusive tax split, the deterministic PDF stored in the invoice archive
//! (the same `RawArchive` the download route reads), and the row stamped with all of it. If any
//! step fails the transaction rolls back, the event id is not consumed, and Razorpay's retry gets
//! a clean attempt (the 500, as Python).
//!
//! Nothing here can place, modify or cancel an order on a broker (law 2): a Razorpay "order" is
//! a payment intent.

use std::time::Duration;

use super::invoice;

use axum::extract::{Extension, Request, State};
use axum::http::HeaderMap;
use axum::response::Response;
use baskfy_db::{OrderedJson, row};
use chrono::{DateTime, Utc};
use hmac::{Hmac, Mac};
use rust_decimal::Decimal;
use serde::Serialize;
use sha2::Sha256;
use sqlx::Row;

use crate::auth::{Principal, require_authenticated};
use crate::body::Received;
use crate::logging;
use crate::problems::{ErrorEntry, Problem, ProblemType, internal_error};
use crate::routes::api_keys::model::{
    Field, Kind, Presence, dbw, field, now_micro, raw_body, validate, write_pool,
};
use crate::session::{self, Tx, WritePool};
use crate::settings::Settings;
use crate::state::AppState;
use crate::wire;

/// `SIGNATURE_HEADER`.
pub const SIGNATURE_HEADER: &str = "X-Razorpay-Signature";
/// `EVENT_ID_HEADER`.
pub const EVENT_ID_HEADER: &str = "X-Razorpay-Event-Id";
/// `PAST_DUE`.
const PAST_DUE: &str = "past_due";
/// `ACTIVE`.
const ACTIVE: &str = "active";
/// `NOTE_USER`.
const NOTE_USER: &str = "baskfy_user_public_id";
/// `NOTE_PLAN`.
const NOTE_PLAN: &str = "baskfy_plan_code";

/// `SUBSCRIPTION_EVENT_STATUS`.
fn subscription_event_status(event: &str) -> Option<&'static str> {
    Some(match event {
        "subscription.authenticated"
        | "subscription.pending"
        | "subscription.halted"
        | "subscription.paused" => PAST_DUE,
        "subscription.activated"
        | "subscription.charged"
        | "subscription.resumed"
        | "subscription.updated" => ACTIVE,
        "subscription.cancelled" => "cancelled",
        "subscription.completed" | "subscription.expired" => "expired",
        _ => return None,
    })
}

/// `PAYMENT_EVENTS`.
fn is_payment_event(event: &str) -> bool {
    matches!(
        event,
        "payment.captured" | "order.paid" | "subscription.charged"
    )
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// `expected_signature(payload, secret)`.
fn expected_signature(payload: &[u8], secret: &str) -> String {
    match Hmac::<Sha256>::new_from_slice(secret.as_bytes()) {
        Ok(mut mac) => {
            mac.update(payload);
            hex(&mac.finalize().into_bytes())
        }
        Err(_) => String::new(),
    }
}

/// `hmac.compare_digest(a, b)` over two ASCII strings, in constant time for equal lengths.
fn compare_digest(a: &str, b: &str) -> bool {
    let (a, b) = (a.as_bytes(), b.as_bytes());
    let mut diff = u8::from(a.len() != b.len());
    for (x, y) in a.iter().zip(b) {
        diff |= x ^ y;
    }
    diff == 0
}

// ---------------------------------------------------------------------------------------------
// Python's str() of what json.loads produced
// ---------------------------------------------------------------------------------------------

fn py_repr(v: &OrderedJson) -> String {
    match v {
        OrderedJson::Str(s) => baskfy_providers::py::repr_str(s),
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
        other => py_str(other),
    }
}

/// `str(value)`.
fn py_str(v: &OrderedJson) -> String {
    match v {
        OrderedJson::Null => "None".to_owned(),
        OrderedJson::Bool(true) => "True".to_owned(),
        OrderedJson::Bool(false) => "False".to_owned(),
        OrderedJson::Int(i) => i.to_string(),
        OrderedJson::UInt(u) => u.to_string(),
        OrderedJson::BigInt(s) => s.clone(),
        OrderedJson::Float(f) if f.is_nan() => "nan".to_owned(),
        OrderedJson::Float(f) if f.is_infinite() => {
            if *f > 0.0 { "inf" } else { "-inf" }.to_owned()
        }
        OrderedJson::Float(f) => baskfy_core::py::float_repr(*f),
        OrderedJson::Str(s) => s.clone(),
        other => py_repr(other),
    }
}

/// `_entity(payload, kind)`: `payload.<kind>.entity`, or an empty mapping.
fn entity<'a>(payload: &'a OrderedJson, kind: &str) -> Option<&'a [(String, OrderedJson)]> {
    let holder = payload.get("payload")?.get(kind)?;
    match holder.get("entity")? {
        OrderedJson::Object(entries) => Some(entries),
        _ => None,
    }
}

fn member<'a>(entity: Option<&'a [(String, OrderedJson)]>, key: &str) -> Option<&'a OrderedJson> {
    entity?.iter().rev().find(|(k, _)| k == key).map(|(_, v)| v)
}

/// `_text(entity, key)`: a `str` or an `int` (a `bool` is an `int` in Python) as text.
fn text(entity: Option<&[(String, OrderedJson)]>, key: &str) -> Option<String> {
    match member(entity, key)? {
        OrderedJson::Str(s) => Some(s.clone()),
        v @ (OrderedJson::Int(_)
        | OrderedJson::UInt(_)
        | OrderedJson::BigInt(_)
        | OrderedJson::Bool(_)) => Some(py_str(v)),
        _ => None,
    }
}

/// `_period_end(entity)`: the first positive epoch of `current_end`, `end_at`, `charge_at`.
fn period_end(entity: Option<&[(String, OrderedJson)]>) -> Result<Option<DateTime<Utc>>, Problem> {
    for key in ["current_end", "end_at", "charge_at"] {
        let seconds = match member(entity, key) {
            Some(OrderedJson::Int(i)) => Some(*i),
            Some(OrderedJson::Bool(true)) => Some(1),
            Some(OrderedJson::BigInt(digits)) if digits.starts_with('-') => None,
            Some(OrderedJson::UInt(_) | OrderedJson::BigInt(_)) => {
                // `datetime.fromtimestamp` overflows: the catch-all.
                return Err(unhandled("year is out of range"));
            }
            _ => None,
        };
        if let Some(s) = seconds
            && s > 0
        {
            return DateTime::from_timestamp(s, 0)
                .filter(|t| chrono::Datelike::year(t) <= 9999)
                .map(Some)
                .ok_or_else(|| unhandled("year is out of range"));
        }
    }
    Ok(None)
}

fn unhandled(reason: &str) -> Problem {
    logging::error("baskfy_api.app", "unhandled error", &[("error", reason)]);
    internal_error()
}

// ---------------------------------------------------------------------------------------------
// The gateway
// ---------------------------------------------------------------------------------------------

/// What a gateway call raised.
enum GatewayError {
    /// `PaymentsNotConfigured`.
    NotConfigured,
    /// `RazorpayError`.
    Refused(String),
    /// Anything else `_post` lets escape — `response.json()` on a body that is not JSON raises
    /// `JSONDecodeError`, which no handler catches: the 500 catch-all, not the 503.
    Unhandled(String),
}

/// `RazorpayGateway._post(path, body)`.
async fn gateway_post(
    settings: &Settings,
    path: &str,
    body: OrderedJson,
) -> Result<serde_json::Map<String, serde_json::Value>, GatewayError> {
    let secret = settings.razorpay_key_secret.expose();
    if settings.razorpay_key_id.is_empty() || secret.is_empty() {
        return Err(GatewayError::NotConfigured);
    }
    let url = format!("{}{path}", settings.razorpay_api_base.trim_end_matches('/'));
    let timeout = Duration::from_secs_f64(settings.razorpay_timeout_seconds.max(0.001));
    let client = baskfy_providers::net::GuardedClient::from_builder(
        baskfy_providers::net::client_builder().timeout(timeout),
    )
    .map_err(|e| GatewayError::Refused(e.to_string()))?;
    let auth = {
        use base64::Engine;
        base64::engine::general_purpose::STANDARD
            .encode(format!("{}:{secret}", settings.razorpay_key_id).as_bytes())
    };
    // httpx's `json=`: `json.dumps(..., ensure_ascii=False, separators=(",", ":"))`, key order
    // kept.
    let payload = body
        .to_python_json()
        .map_err(|e| GatewayError::Refused(e.to_string()))?
        .into_bytes();
    let request = client
        .post(&url)
        .map_err(|e| GatewayError::Refused(format!("could not reach Razorpay: {e}")))?
        .header("Authorization", format!("Basic {auth}"))
        .header("Content-Type", "application/json")
        .body(payload);
    let response = request
        .send()
        .await
        .map_err(|e| GatewayError::Refused(format!("could not reach Razorpay: {e}")))?;
    let status = response.status().as_u16();
    let text = response
        .text()
        .await
        .map_err(|e| GatewayError::Refused(format!("could not reach Razorpay: {e}")))?;
    if status >= 400 {
        let cut: String = text.chars().take(300).collect();
        return Err(GatewayError::Refused(format!(
            "Razorpay refused {path} with {status}: {cut}"
        )));
    }
    match serde_json::from_str::<serde_json::Value>(&text) {
        Ok(serde_json::Value::Object(map)) => Ok(map),
        Ok(_) => Err(GatewayError::Refused(format!(
            "Razorpay returned a non-object for {path}"
        ))),
        // `response.json()` raising is not a `RazorpayError`: the 500 catch-all.
        Err(e) => Err(GatewayError::Unhandled(format!(
            "Razorpay answered {path} with a body that is not JSON: {e}"
        ))),
    }
}

/// A JSON object in insertion order (a Python `dict` literal).
fn obj(fields: Vec<(&str, OrderedJson)>) -> OrderedJson {
    OrderedJson::Object(fields.into_iter().map(|(k, v)| (k.to_owned(), v)).collect())
}

/// `str(order.get("id", ""))` and friends.
fn id_text(map: &serde_json::Map<String, serde_json::Value>) -> Option<String> {
    map.get("id").map(|v| match v {
        serde_json::Value::String(s) => s.clone(),
        serde_json::Value::Null => "None".to_owned(),
        serde_json::Value::Bool(true) => "True".to_owned(),
        serde_json::Value::Bool(false) => "False".to_owned(),
        other => other.to_string(),
    })
}

fn id_truthy(map: &serde_json::Map<String, serde_json::Value>) -> bool {
    match map.get("id") {
        None | Some(serde_json::Value::Null | serde_json::Value::Bool(false)) => false,
        Some(serde_json::Value::String(s)) => !s.is_empty(),
        Some(serde_json::Value::Number(n)) => n.as_f64() != Some(0.0),
        Some(serde_json::Value::Array(a)) => !a.is_empty(),
        Some(serde_json::Value::Object(o)) => !o.is_empty(),
        Some(serde_json::Value::Bool(true)) => true,
    }
}

// ---------------------------------------------------------------------------------------------
// POST /checkout/session
// ---------------------------------------------------------------------------------------------

/// `CheckoutSessionOut`.
#[derive(Debug, Clone, Serialize)]
struct CheckoutSessionOut {
    plan_code: String,
    kind: &'static str,
    #[serde(serialize_with = "wire::ser::decimal")]
    amount_inr: Decimal,
    currency: &'static str,
    key_id: String,
    order_id: Option<String>,
    subscription_id: Option<String>,
    disclosure: Option<String>,
}

/// `CheckoutSessionIn`.
const CHECKOUT: [Field; 1] = [field(
    "plan_code",
    Kind::Str {
        min: Some(1),
        max: Some(32),
    },
    Presence::Required,
)];

/// `_upsert_pending_subscription(session, user, plan, razorpay_subscription_id)`: the id.
async fn upsert_pending(
    tx: &mut Tx,
    user_id: i64,
    plan_id: i16,
    gateway_id: Option<&str>,
) -> Result<i64, Problem> {
    let existing: Option<(i64, Option<String>)> = sqlx::query_as(
        "SELECT id, razorpay_subscription_id FROM subscription WHERE user_id = $1 \
         AND plan_id = $2 AND status = $3 ORDER BY id DESC LIMIT 1",
    )
    .bind(user_id)
    .bind(plan_id)
    .bind(PAST_DUE)
    .fetch_optional(&mut **tx)
    .await
    .map_err(dbw)?;
    if let Some((id, current)) = existing {
        if let Some(g) = gateway_id
            && !g.is_empty()
            && current.as_deref() != Some(g)
        {
            sqlx::query("UPDATE subscription SET razorpay_subscription_id = $2 WHERE id = $1")
                .bind(id)
                .bind(g)
                .execute(&mut **tx)
                .await
                .map_err(dbw)?;
        }
        return Ok(id);
    }
    sqlx::query_scalar(
        "INSERT INTO subscription (user_id, plan_id, status, started_at, current_period_end, \
         razorpay_subscription_id) VALUES ($1, $2, $3, $4, NULL, $5) RETURNING id",
    )
    .bind(user_id)
    .bind(plan_id)
    .bind(PAST_DUE)
    .bind(now_micro())
    .bind(gateway_id)
    .fetch_one(&mut **tx)
    .await
    .map_err(dbw)
}

fn checkout_error(detail: String) -> Problem {
    Problem::new(ProblemType::NotFound, detail)
}

fn gateway_problem(e: GatewayError, plan_code: &str) -> Problem {
    match e {
        GatewayError::NotConfigured => Problem::new(
            ProblemType::InternalError,
            "Payments are not configured on this deployment.",
        ),
        GatewayError::Refused(reason) => {
            logging::error(
                "baskfy_api.routers.billing",
                "razorpay refused a checkout",
                &[("plan", plan_code), ("reason", &reason)],
            );
            Problem::new(
                ProblemType::PipelineDegraded,
                "The payment gateway is not responding.",
            )
        }
        GatewayError::Unhandled(reason) => unhandled(&reason),
    }
}

/// `POST /checkout/session` (`create_checkout_session`).
///
/// # Errors
/// 400 (body), 401, 404 (no such plan; a plan not on sale; a missing gateway plan id), 500
/// (payments not configured), 503 (the gateway refused or could not be reached).
pub async fn create_checkout_session(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    request: Request,
) -> Result<Response, Problem> {
    let (content_type, bytes) = raw_body(request).await?;
    let received = crate::body::receive(content_type.as_deref(), &bytes)?;
    let principal = require_authenticated(&principal)?;
    let values = validate(&received, &CHECKOUT)?;
    let plan_code = values[0].str().unwrap_or_default().to_owned();
    let user_id = principal.require_user()?;
    let pool = write_pool(write)?;
    let settings = state.settings.clone();
    let out = session::run(&pool, move |tx| {
        Box::pin(async move {
            let user = sqlx::query(
                "SELECT public_id, name, email::text AS email FROM app_user WHERE id = $1",
            )
            .bind(user_id)
            .fetch_optional(&mut **tx)
            .await
            .map_err(dbw)?
            .ok_or_else(|| {
                Problem::new(
                    ProblemType::Unauthenticated,
                    "That account no longer exists.",
                )
            })?;
            let user_public_id: String = user.try_get("public_id").map_err(dbw)?;
            let user_name: Option<String> = user.try_get("name").map_err(dbw)?;
            let user_email: String = user.try_get("email").map_err(dbw)?;
            let plan = sqlx::query(
                "SELECT id, code, price_inr::text AS price, interval, features FROM plan \
                 WHERE code = $1",
            )
            .bind(&plan_code)
            .fetch_optional(&mut **tx)
            .await
            .map_err(dbw)?;
            let Some(plan) = plan else {
                return Err(checkout_error(format!(
                    "No plan with code {}.",
                    baskfy_providers::py::repr_str(&plan_code)
                )));
            };
            let plan_id: i16 = plan.try_get("id").map_err(dbw)?;
            let code: String = plan.try_get("code").map_err(dbw)?;
            let price: Decimal = plan
                .try_get::<String, _>("price")
                .map_err(dbw)?
                .parse()
                .map_err(|_| internal_error())?;
            let interval: Option<String> = plan.try_get("interval").map_err(dbw)?;
            let features = row::jsonb(&plan, "features").map_err(|e| crate::db_failure(&e))?;
            if price <= Decimal::ZERO && !settings.free_tier_enabled {
                return Err(checkout_error(format!(
                    "The {} plan is not on sale.",
                    baskfy_providers::py::repr_str(&plan_code)
                )));
            }
            if price <= Decimal::ZERO {
                return Err(checkout_error(
                    "The free plan does not need a checkout; it is granted on sign-up.".to_owned(),
                ));
            }
            let disclosure = match features.get("disclosure") {
                Some(OrderedJson::Str(s)) => Some(s.clone()),
                _ => None,
            };
            let notes = obj(vec![
                (NOTE_USER, OrderedJson::Str(user_public_id.clone())),
                (NOTE_PLAN, OrderedJson::Str(code.clone())),
            ]);
            if interval.is_none() {
                let order = gateway_post(
                    &settings,
                    "/orders",
                    obj(vec![
                        ("amount", OrderedJson::Int(to_paise(price)?)),
                        ("currency", OrderedJson::Str("INR".to_owned())),
                        (
                            "receipt",
                            OrderedJson::Str(format!("{user_public_id}-{code}")),
                        ),
                        ("notes", notes),
                        ("payment_capture", OrderedJson::Int(1)),
                    ]),
                )
                .await
                .map_err(|e| gateway_problem(e, &plan_code))?;
                upsert_pending(tx, user_id, plan_id, None).await?;
                return Ok(CheckoutSessionOut {
                    plan_code: code,
                    kind: "order",
                    amount_inr: price,
                    currency: "INR",
                    key_id: settings.razorpay_key_id.clone(),
                    order_id: Some(id_text(&order).unwrap_or_default()),
                    subscription_id: None,
                    disclosure,
                });
            }
            let monthly = interval.as_deref() == Some("month");
            let customer = gateway_post(
                &settings,
                "/customers",
                obj(vec![
                    (
                        "name",
                        OrderedJson::Str(
                            user_name
                                .filter(|n| !n.is_empty())
                                .unwrap_or_else(|| user_email.clone()),
                        ),
                    ),
                    ("email", OrderedJson::Str(user_email.clone())),
                    ("fail_existing", OrderedJson::Int(0)),
                ]),
            )
            .await
            .map_err(|e| gateway_problem(e, &plan_code))?;
            let gateway_plan = if monthly {
                &settings.razorpay_plan_id_monthly
            } else {
                &settings.razorpay_plan_id_yearly
            };
            if gateway_plan.is_empty() {
                return Err(checkout_error(format!(
                    "No Razorpay plan id is configured for the {} plan; set \
                     BASKFY_RAZORPAY_PLAN_ID_{}.",
                    baskfy_providers::py::repr_str(&code),
                    if monthly { "MONTHLY" } else { "YEARLY" }
                )));
            }
            let mut fields = vec![
                ("plan_id", OrderedJson::Str(gateway_plan.clone())),
                (
                    "total_count",
                    OrderedJson::Int(if monthly {
                        settings.razorpay_subscription_cycles_monthly
                    } else {
                        settings.razorpay_subscription_cycles_yearly
                    }),
                ),
                ("customer_notify", OrderedJson::Int(1)),
                ("notes", notes),
            ];
            if id_truthy(&customer)
                && let Some(customer_id) = id_text(&customer)
            {
                fields.push(("customer_id", OrderedJson::Str(customer_id)));
            }
            let body = obj(fields);
            let created = gateway_post(&settings, "/subscriptions", body)
                .await
                .map_err(|e| gateway_problem(e, &plan_code))?;
            let gateway_id = id_text(&created).unwrap_or_default();
            let gid = (!gateway_id.is_empty()).then_some(gateway_id.as_str());
            let subscription_id = upsert_pending(tx, user_id, plan_id, gid).await?;
            if id_truthy(&customer) {
                sqlx::query(
                    "UPDATE subscription SET razorpay_customer_id = $2 WHERE id = $1 \
                     AND razorpay_customer_id IS DISTINCT FROM $2",
                )
                .bind(subscription_id)
                .bind(id_text(&customer))
                .execute(&mut **tx)
                .await
                .map_err(dbw)?;
            }
            Ok(CheckoutSessionOut {
                plan_code: code,
                kind: "subscription",
                amount_inr: price,
                currency: "INR",
                key_id: settings.razorpay_key_id.clone(),
                order_id: None,
                subscription_id: gid.map(str::to_owned),
                disclosure,
            })
        })
    })
    .await?;
    Ok(wire::json(&out))
}

/// `to_paise(amount_inr)`: exact.
fn to_paise(amount: Decimal) -> Result<i64, Problem> {
    let paise = amount * Decimal::ONE_HUNDRED;
    if paise.fract() != Decimal::ZERO {
        return Err(unhandled("not a whole number of paise"));
    }
    i64::try_from(paise.trunc()).map_err(|_| unhandled("paise out of range"))
}

// ---------------------------------------------------------------------------------------------
// POST /webhooks/razorpay
// ---------------------------------------------------------------------------------------------

/// `WebhookAck`.
#[derive(Debug, Clone, Serialize)]
struct WebhookAck {
    status: &'static str,
}

/// `WebhookOutcome`.
struct Outcome {
    status: &'static str,
    detail: String,
}

fn header(headers: &HeaderMap, name: &str) -> Option<String> {
    headers
        .get(name)
        .map(|v| v.as_bytes().iter().map(|&b| char::from(b)).collect())
}

/// `_apply_subscription_lifecycle(session, event_type, payload)`.
async fn lifecycle(
    tx: &mut Tx,
    status: &'static str,
    payload: &OrderedJson,
) -> Result<Outcome, Problem> {
    let entity = entity(payload, "subscription");
    let Some(gateway_id) = text(entity, "id") else {
        return Ok(Outcome {
            status: "ignored",
            detail: "No subscription id on the event.".to_owned(),
        });
    };
    let rows: Vec<(i64, String, Option<DateTime<Utc>>)> = sqlx::query_as(
        "SELECT id, status, current_period_end FROM subscription \
         WHERE razorpay_subscription_id = $1",
    )
    .bind(&gateway_id)
    .fetch_all(&mut **tx)
    .await
    .map_err(dbw)?;
    let (id, old_status, old_end) = match rows.as_slice() {
        [] => {
            return Ok(Outcome {
                status: "ignored",
                detail: format!(
                    "No subscription matches {}.",
                    baskfy_providers::py::repr_str(&gateway_id)
                ),
            });
        }
        [one] => one.clone(),
        _ => {
            return Err(unhandled(
                "Multiple rows were found when one or none was required",
            ));
        }
    };
    let end = period_end(entity)?;
    let new_end = end.or(old_end);
    if old_status != status || new_end != old_end {
        sqlx::query("UPDATE subscription SET status = $2, current_period_end = $3 WHERE id = $1")
            .bind(id)
            .bind(status)
            .bind(new_end)
            .execute(&mut **tx)
            .await
            .map_err(dbw)?;
    }
    Ok(Outcome {
        status: "processed",
        detail: format!("{gateway_id} is now {status}."),
    })
}

/// A subscription row as `_grant_for` returns it.
struct Grant {
    id: i64,
    started_at: DateTime<Utc>,
    current_period_end: Option<DateTime<Utc>>,
}

/// A plan row.
struct PlanRow {
    code: String,
    interval: Option<String>,
    price: Decimal,
}

async fn plan_by(
    tx: &mut Tx,
    column: &str,
    value: PlanKey<'_>,
) -> Result<Option<(i16, PlanRow)>, Problem> {
    let sql = format!(
        "SELECT id, code, interval, price_inr::text AS price FROM plan WHERE {column} = $1"
    );
    let q = sqlx::query(&sql);
    let q = match value {
        PlanKey::Id(id) => q.bind(id),
        PlanKey::Code(code) => q.bind(code.to_owned()),
    };
    let Some(r) = q.fetch_optional(&mut **tx).await.map_err(dbw)? else {
        return Ok(None);
    };
    let price: String = r.try_get("price").map_err(dbw)?;
    Ok(Some((
        r.try_get("id").map_err(dbw)?,
        PlanRow {
            code: r.try_get("code").map_err(dbw)?,
            interval: r.try_get("interval").map_err(dbw)?,
            price: price.parse().map_err(|_| internal_error())?,
        },
    )))
}

/// How a plan is looked up.
enum PlanKey<'a> {
    Id(i16),
    Code(&'a str),
}

async fn grant_row(tx: &mut Tx, id: i64) -> Result<Grant, Problem> {
    let (started_at, current_period_end): (DateTime<Utc>, Option<DateTime<Utc>>) =
        sqlx::query_as("SELECT started_at, current_period_end FROM subscription WHERE id = $1")
            .bind(id)
            .fetch_one(&mut **tx)
            .await
            .map_err(dbw)?;
    Ok(Grant {
        id,
        started_at,
        current_period_end,
    })
}

/// `_grant_for(session, payload, entity, user)`: the subscription the event names (promoted
/// from `past_due`), else the user's grant for the plan in `notes` (created or promoted).
async fn grant_for(
    tx: &mut Tx,
    payload: &OrderedJson,
    payment: Option<&[(String, OrderedJson)]>,
    user: Option<i64>,
) -> Result<(Option<Grant>, Option<PlanRow>), Problem> {
    let subscription_entity = entity(payload, "subscription");
    // Python's `or`: an empty subscription id is falsy, so the payment's `subscription_id` is
    // consulted (and an empty one there is still looked up — `is not None`).
    let gateway_id = text(subscription_entity, "id")
        .filter(|id| !id.is_empty())
        .or_else(|| text(payment, "subscription_id"));
    if let Some(gid) = gateway_id {
        let rows: Vec<(i64, i16, String)> = sqlx::query_as(
            "SELECT id, plan_id, status FROM subscription WHERE razorpay_subscription_id = $1",
        )
        .bind(&gid)
        .fetch_all(&mut **tx)
        .await
        .map_err(dbw)?;
        if rows.len() > 1 {
            return Err(unhandled(
                "Multiple rows were found when one or none was required",
            ));
        }
        if let Some((id, plan_id, status)) = rows.first() {
            let plan = plan_by(tx, "id", PlanKey::Id(*plan_id))
                .await?
                .map(|(_, p)| p);
            if status == PAST_DUE {
                sqlx::query("UPDATE subscription SET status = $2 WHERE id = $1")
                    .bind(id)
                    .bind(ACTIVE)
                    .execute(&mut **tx)
                    .await
                    .map_err(dbw)?;
            }
            return Ok((Some(grant_row(tx, *id).await?), plan));
        }
    }
    let notes = member(payment, "notes");
    let plan_code = match notes {
        Some(OrderedJson::Object(_)) => match notes.and_then(|n| n.get(NOTE_PLAN)) {
            Some(OrderedJson::Str(c)) => Some(c.clone()),
            _ => None,
        },
        _ => None,
    };
    let (Some(code), Some(user_id)) = (plan_code, user) else {
        return Ok((None, None));
    };
    let Some((plan_id, plan)) = plan_by(tx, "code", PlanKey::Code(&code)).await? else {
        return Ok((None, None));
    };
    let pending: Option<(i64, String)> = sqlx::query_as(
        "SELECT id, status FROM subscription WHERE user_id = $1 AND plan_id = $2 \
         ORDER BY id DESC LIMIT 1",
    )
    .bind(user_id)
    .bind(plan_id)
    .fetch_optional(&mut **tx)
    .await
    .map_err(dbw)?;
    let id = match pending {
        None => sqlx::query_scalar(
            "INSERT INTO subscription (user_id, plan_id, status, started_at, current_period_end) \
             VALUES ($1, $2, $3, $4, NULL) RETURNING id",
        )
        .bind(user_id)
        .bind(plan_id)
        .bind(ACTIVE)
        .bind(now_micro())
        .fetch_one(&mut **tx)
        .await
        .map_err(dbw)?,
        Some((id, status)) => {
            if status != ACTIVE {
                sqlx::query("UPDATE subscription SET status = $2 WHERE id = $1")
                    .bind(id)
                    .bind(ACTIVE)
                    .execute(&mut **tx)
                    .await
                    .map_err(dbw)?;
            }
            id
        }
    };
    Ok((Some(grant_row(tx, id).await?), Some(plan)))
}

/// `_record_payment(session, context, payload)`: one captured charge, one `payment` row and one
/// tax invoice; `None` when the event carries no payment.
async fn record_payment(
    tx: &mut Tx,
    settings: &Settings,
    payload: &OrderedJson,
) -> Result<Option<Outcome>, Problem> {
    let payment = entity(payload, "payment");
    let Some(payment_id) = text(payment, "id") else {
        return Ok(None);
    };
    if let Some(status) = text(payment, "status")
        && status != "captured"
    {
        return Ok(Some(Outcome {
            status: "ignored",
            detail: format!("{payment_id} is not captured."),
        }));
    }
    let existing: Vec<i64> =
        sqlx::query_scalar("SELECT id FROM payment WHERE razorpay_payment_id = $1")
            .bind(&payment_id)
            .fetch_all(&mut **tx)
            .await
            .map_err(dbw)?;
    if existing.len() > 1 {
        return Err(unhandled(
            "Multiple rows were found when one or none was required",
        ));
    }
    if !existing.is_empty() {
        return Ok(Some(Outcome {
            status: "ignored",
            detail: format!("{payment_id} is already recorded."),
        }));
    }
    // `_user_from_notes`.
    let notes = member(payment, "notes");
    let user: Option<(i64, Option<String>, String)> = match notes {
        Some(OrderedJson::Object(_)) => match notes.and_then(|n| n.get(NOTE_USER)) {
            Some(OrderedJson::Str(public_id)) => {
                let rows: Vec<(i64, Option<String>, String)> = sqlx::query_as(
                    "SELECT id, name, email::text FROM app_user WHERE public_id = $1",
                )
                .bind(public_id)
                .fetch_all(&mut **tx)
                .await
                .map_err(dbw)?;
                rows.into_iter().next()
            }
            _ => None,
        },
        _ => None,
    };
    let (grant, plan) = grant_for(tx, payload, payment, user.as_ref().map(|u| u.0)).await?;
    let (Some((user_id, user_name, user_email)), Some(plan)) = (user, plan) else {
        return Ok(Some(Outcome {
            status: "ignored",
            detail: format!("{payment_id} could not be attributed to an account."),
        }));
    };
    let amount_inr = match member(payment, "amount") {
        Some(OrderedJson::Int(paise)) => Decimal::new(*paise, 2),
        Some(OrderedJson::Bool(b)) => Decimal::new(i64::from(*b), 2),
        Some(OrderedJson::UInt(_) | OrderedJson::BigInt(_)) => {
            // Beyond numeric(12,2): asyncpg/Postgres refuse it — the catch-all.
            return Err(unhandled("numeric field overflow"));
        }
        _ => plan.price,
    };
    let order_id = text(payment, "order_id");
    let payment_row: i64 = sqlx::query_scalar(
        "INSERT INTO payment (user_id, subscription_id, amount_inr, status, razorpay_payment_id, \
         razorpay_order_id, customer_gstin, place_of_supply) \
         VALUES ($1, $2, $3, 'captured', $4, $5, NULL, NULL) RETURNING id",
    )
    .bind(user_id)
    .bind(grant.as_ref().map(|g| g.id))
    .bind(amount_inr)
    .bind(&payment_id)
    .bind(&order_id)
    .fetch_one(&mut **tx)
    .await
    .map_err(dbw)?;
    // `issue_invoice`.
    let issued_on = crate::routes::common::today_ist();
    let place = if settings.supplier_state.is_empty() {
        "Unknown".to_owned()
    } else {
        settings.supplier_state.clone()
    };
    let intra_state = settings.supplier_state.is_empty() || place == settings.supplier_state;
    let tax = invoice::split_inclusive(amount_inr, settings.gst_rate_percent, intra_state);
    let number = invoice::allocate_number(tx, settings, issued_on).await?;
    let period = grant.as_ref().and_then(|g| {
        g.current_period_end
            .map(|end| (g.started_at.date_naive(), end.date_naive()))
    });
    let document = invoice::Document {
        invoice_number: number.clone(),
        invoice_date: issued_on,
        supplier: invoice::Party {
            name: settings.supplier_legal_name.clone(),
            address_lines: settings.supplier_address_lines.clone(),
            gstin: Some(settings.supplier_gstin.clone()).filter(|g| !g.is_empty()),
            state: Some(settings.supplier_state.clone()).filter(|s| !s.is_empty()),
            email: None,
        },
        recipient: invoice::Party {
            name: user_name
                .filter(|n| !n.is_empty())
                .unwrap_or_else(|| user_email.clone()),
            address_lines: Vec::new(),
            gstin: None,
            state: None,
            email: Some(user_email),
        },
        description: invoice::describe(&plan.code, plan.interval.is_some()),
        sac_code: settings.gst_sac_code.clone(),
        place_of_supply: place.clone(),
        tax: tax.clone(),
        period,
        payment_reference: Some(payment_id.clone()),
    };
    let key = format!("{}/{number}.pdf", settings.invoice_object_prefix);
    let pdf = invoice::render_invoice_pdf(&document);
    let provider_settings = baskfy_providers::settings::ProviderSettings::from_env()
        .map_err(|e| unhandled(&e.to_string()))?;
    let archive = baskfy_providers::factory::build_archive(
        &provider_settings,
        Some(std::path::Path::new(&settings.invoice_local_dir)),
    )
    .map_err(|e| unhandled(&e.to_string()))?;
    archive
        .put(&key, &pdf, "application/pdf")
        .await
        .map_err(|e| unhandled(&e.to_string()))?;
    sqlx::query(
        "UPDATE payment SET invoice_number = $2, invoice_date = $3, invoice_pdf_key = $4, \
         taxable_inr = $5, cgst_inr = $6, sgst_inr = $7, igst_inr = $8, gst_inr = $9, \
         gst_rate = $10, place_of_supply = $11, sac_code = $12 WHERE id = $1",
    )
    .bind(payment_row)
    .bind(&number)
    .bind(issued_on)
    .bind(&key)
    .bind(tax.taxable)
    .bind(tax.cgst)
    .bind(tax.sgst)
    .bind(tax.igst)
    .bind(tax.tax())
    .bind(tax.rate)
    .bind(&place)
    .bind(&settings.gst_sac_code)
    .execute(&mut **tx)
    .await
    .map_err(dbw)?;
    Ok(Some(Outcome {
        status: "processed",
        detail: format!("Recorded {payment_id} as {number}."),
    }))
}

/// `POST /webhooks/razorpay` (`razorpay_webhook`).
///
/// # Errors
/// 400 (a bad signature; a body that is not a JSON object), 500 (a body that is not JSON, or
/// any failure while processing — rolled back, so Razorpay retries).
pub async fn razorpay_webhook(
    State(state): State<AppState>,
    write: Option<Extension<WritePool>>,
    request: Request,
) -> Result<Response, Problem> {
    let headers = request.headers().clone();
    let signature = header(&headers, SIGNATURE_HEADER);
    let event_id = header(&headers, EVENT_ID_HEADER);
    let (_, raw) = raw_body(request).await?;
    let secret = state.settings.razorpay_webhook_secret.expose();
    let verified = match signature.as_deref() {
        Some(sig) if !secret.is_empty() && !sig.is_empty() => {
            if !sig.is_ascii() {
                // `hmac.compare_digest` refuses a non-ASCII str: TypeError, the catch-all.
                return Err(unhandled(
                    "comparing strings with non-ASCII characters is not supported",
                ));
            }
            compare_digest(&expected_signature(&raw, secret), sig)
        }
        _ => false,
    };
    if !verified {
        logging::warning(
            "baskfy_api.routers.billing",
            "rejected a webhook with an invalid signature",
            &[],
        );
        let errors: Vec<ErrorEntry> = vec![vec![
            (
                "field".to_owned(),
                OrderedJson::Str(SIGNATURE_HEADER.to_owned()),
            ),
            (
                "message".to_owned(),
                OrderedJson::Str("signature mismatch".to_owned()),
            ),
        ]];
        return Err(Problem::new(
            ProblemType::InvalidScreenDefinition,
            "The webhook signature did not verify.",
        )
        .with(
            "errors",
            OrderedJson::Array(errors.into_iter().map(OrderedJson::Object).collect()),
        ));
    }
    // `await request.json()`: any failure to decode is an unhandled exception.
    let payload = match crate::body::receive(Some("application/json"), &raw) {
        Ok(Received::Json(v)) => v,
        _ => return Err(unhandled("the webhook body is not JSON")),
    };
    if !matches!(payload, OrderedJson::Object(_)) {
        let errors: Vec<ErrorEntry> = vec![vec![
            ("field".to_owned(), OrderedJson::Str("body".to_owned())),
            (
                "message".to_owned(),
                OrderedJson::Str("expected an object".to_owned()),
            ),
        ]];
        return Err(Problem::new(
            ProblemType::InvalidScreenDefinition,
            "The webhook body was not a JSON object.",
        )
        .with(
            "errors",
            OrderedJson::Array(errors.into_iter().map(OrderedJson::Object).collect()),
        ));
    }
    let key = match event_id {
        Some(id) if !id.is_empty() => id,
        _ => format!("sig:{}", signature.unwrap_or_default()),
    };
    let event_type = {
        let raw_type = payload.get("event").map_or_else(String::new, py_str);
        if raw_type.is_empty() {
            "unknown".to_owned()
        } else {
            raw_type
        }
    };
    let stored = payload
        .to_python_json()
        .map_err(|_| unhandled("Out of range float values are not JSON compliant"))?;
    let pool = write_pool(write)?;
    let settings = state.settings.clone();
    let outcome = session::run(&pool, move |tx| {
        Box::pin(async move {
            let claimed: Option<i64> = sqlx::query_scalar(
                "INSERT INTO webhook_event (provider, event_id, event_type, payload, status) \
                 VALUES ('razorpay', $1, $2, $3::jsonb, 'received') \
                 ON CONFLICT (event_id) DO NOTHING RETURNING id",
            )
            .bind(&key)
            .bind(&event_type)
            .bind(&stored)
            .fetch_optional(&mut **tx)
            .await
            .map_err(dbw)?;
            if claimed.is_none() {
                return Ok(Outcome {
                    status: "ignored",
                    detail: "Already processed this event.".to_owned(),
                });
            }
            let mut life = None;
            if let Some(status) = subscription_event_status(&event_type) {
                life = Some(lifecycle(tx, status, &payload).await?);
            }
            let mut outcome = None;
            if is_payment_event(&event_type) {
                outcome = record_payment(tx, &settings, &payload).await?;
            }
            let outcome = outcome.or(life).unwrap_or_else(|| Outcome {
                status: "ignored",
                detail: format!("{event_type} needs no action."),
            });
            let note: String = outcome.detail.chars().take(500).collect();
            sqlx::query(
                "UPDATE webhook_event SET status = $2, note = $3, processed_at = $4 \
                 WHERE event_id = $1",
            )
            .bind(&key)
            .bind(outcome.status)
            .bind(&note)
            .bind(now_micro())
            .execute(&mut **tx)
            .await
            .map_err(dbw)?;
            Ok(outcome)
        })
    })
    .await?;
    logging::info(
        "baskfy_api.routers.billing",
        "razorpay webhook",
        &[("outcome", outcome.status)],
    );
    Ok(wire::json(&WebhookAck {
        status: if outcome.status == "processed" {
            "processed"
        } else {
            "ignored"
        },
    }))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    #[test]
    fn the_signature_is_hex_hmac_sha256_of_the_raw_body() {
        // test_razorpay.py: hmac.new(b"secret", b'{"a":1}', sha256).hexdigest()
        assert_eq!(
            expected_signature(b"{\"a\":1}", "secret"),
            "aa9e2e3575f5d7098b6caccd790888c36d5fdb63342a73bada2d6a51747a8494"
        );
        assert!(compare_digest("abc", "abc"));
        assert!(!compare_digest("abc", "abd"));
        assert!(!compare_digest("abc", "abcd"));
    }

    #[test]
    fn events_map_onto_the_four_statuses() {
        assert_eq!(
            subscription_event_status("subscription.halted"),
            Some("past_due")
        );
        assert_eq!(
            subscription_event_status("subscription.charged"),
            Some("active")
        );
        assert_eq!(
            subscription_event_status("subscription.completed"),
            Some("expired")
        );
        assert_eq!(subscription_event_status("payment.captured"), None);
        assert!(is_payment_event("order.paid"));
    }

    #[test]
    fn paise_are_exact() {
        assert_eq!(to_paise("500.00".parse().unwrap()).unwrap(), 50000);
        assert!(to_paise("1.005".parse().unwrap()).is_err());
    }
}
