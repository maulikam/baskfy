//! ``POST /support`` — the destination of the contact form (PROMPTS.md Prompt 18 §2).
//!
//! Ports `baskfy_api.routers.support` (`APIRouter(tags=["support"])`) and the two templates it
//! sends (`email.templates.support_request`, `support_receipt`, with the shared shell in
//! [`templates`]).
//!
//! Unauthenticated on purpose (someone who cannot sign in most needs support); the message can
//! only ever be addressed to *our* inbox (`BASKFY_SUPPORT_EMAIL`) — there is no recipient
//! parameter, so it is not an open relay. Nothing is stored. A delivery failure is the 500
//! `internal-error` ("try again"), because a visitor told "thanks" would not retry; the receipt
//! to the sender is best-effort. No part of the message body is ever logged. **No route may
//! place, modify or cancel an order** (law 2).

pub(crate) mod templates;

use axum::Router;
use axum::extract::{Extension, Request, State};
use axum::http::StatusCode;
use axum::response::Response;
use axum::routing::post;
use serde::Serialize;

use super::api_keys::model::{Field, Kind, Presence, field, read_body, validate};
use crate::auth::Principal;
use crate::logging;
use crate::mailer::Mailer;
use crate::problems::{Problem, ProblemType};
use crate::state::AppState;
use crate::wire;

/// `SupportTopic`.
pub const TOPICS: &[&str] = &[
    "A number looks wrong",
    "Billing or invoices",
    "My account",
    "A feature request",
    "Something else",
];

/// `SupportMessageIn`.
const MESSAGE: [Field; 4] = [
    field(
        "name",
        Kind::Str {
            min: Some(1),
            max: Some(120),
        },
        Presence::Required,
    ),
    field("email", Kind::Email, Presence::Required),
    field("topic", Kind::Literal(TOPICS), Presence::Required),
    field(
        "message",
        Kind::Str {
            min: Some(20),
            max: Some(4000),
        },
        Presence::Required,
    ),
];

/// `AcceptedOut`.
#[derive(Debug, Clone, Serialize)]
struct AcceptedOut {
    status: &'static str,
    detail: &'static str,
}

/// `POST /support` (`send_support_message`): 202 once support has the message.
///
/// # Errors
/// 400 (body), 401 (a bad bearer token — the router's principal dependency), 500 (the message
/// could not be delivered).
pub async fn send_support_message(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    request: Request,
) -> Result<Response, Problem> {
    let received = read_body(request).await?;
    let values = validate(&received, &MESSAGE)?;
    let text = |i: usize| values[i].str().unwrap_or_default().to_owned();
    let (name, email, topic, message) = (text(0), text(1), text(2), text(3));
    let mailer = Mailer::from_settings(&state.settings);
    let request_mail = templates::support_request(
        &state.settings.support_email,
        &templates::SupportSubmission {
            topic: &topic,
            from_name: &name,
            from_email: &email,
            body_text: &message,
            account_public_id: principal.public_id.as_deref(),
        },
    );
    if !mailer.deliver(&request_mail).await {
        logging::warning(
            "baskfy_api.routers.support",
            "support message could not be delivered",
            &[("support_topic", &topic)],
        );
        return Err(Problem::new(
            ProblemType::InternalError,
            "The message could not be sent. Please try again in a moment.",
        ));
    }
    // Best-effort: the message did reach support.
    mailer
        .deliver(&templates::support_receipt(&email, &topic))
        .await;
    Ok(wire::json_status(
        StatusCode::ACCEPTED,
        &AcceptedOut {
            status: "accepted",
            detail: "Thanks — your message has been sent. We reply to every one.",
        },
    ))
}

/// The routes of `baskfy_api.routers.support`, relative to `/api/v1`.
pub fn router() -> Router<AppState> {
    Router::new().route("/support", post(send_support_message))
}
