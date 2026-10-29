//! RFC 9457 `application/problem+json`. Ports `baskfy_api.problems` (docs/07 §"Error catalogue").
//!
//! Every error this service returns is a [`Problem`]: the body is `type`, `title`, `status`,
//! `detail`, `instance`, then the type's extension members **in the order Python passes them**
//! (pydantic is not involved: `Problem.body` builds a `dict` and `JSONResponse` dumps it, so the
//! key order is the insertion order). An extension member whose value is `None` is omitted, as
//! `body()` omits it.

use axum::http::{HeaderName, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use baskfy_db::OrderedJson;

use crate::wire;

/// `CONTENT_TYPE`.
pub const CONTENT_TYPE: &str = "application/problem+json";

/// `UPGRADE_URL` — docs/07: the 402 carries `{"upgrade_url": "/pricing"}`.
pub const UPGRADE_URL: &str = "/pricing";

/// Ports `ProblemType`: docs/07's catalogue, one member per row, in the document's order.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ProblemType {
    /// `invalid-screen-definition` (400).
    InvalidScreenDefinition,
    /// `bad-request` (400).
    BadRequest,
    /// `unauthenticated` (401).
    Unauthenticated,
    /// `payment-required` (402).
    PaymentRequired,
    /// `not-found` (404).
    NotFound,
    /// `stale-data-version` (409).
    StaleDataVersion,
    /// `no-trading-day` (422).
    NoTradingDay,
    /// `rate-limited` (429).
    RateLimited,
    /// `pipeline-degraded` (503).
    PipelineDegraded,
    /// `internal-error` (500).
    InternalError,
    /// `setting-below-floor` (422).
    SettingBelowFloor,
    /// `underlying-not-allowed` (422).
    UnderlyingNotAllowed,
    /// `setting-above-ceiling` (422).
    SettingAboveCeiling,
    /// `scan-in-flight` (409).
    ScanInFlight,
}

impl ProblemType {
    /// Every member, in Python's declaration order (`STATUS_FOR`'s order too).
    pub const ALL: [ProblemType; 14] = [
        Self::InvalidScreenDefinition,
        Self::BadRequest,
        Self::Unauthenticated,
        Self::PaymentRequired,
        Self::NotFound,
        Self::StaleDataVersion,
        Self::NoTradingDay,
        Self::RateLimited,
        Self::PipelineDegraded,
        Self::InternalError,
        Self::SettingBelowFloor,
        Self::UnderlyingNotAllowed,
        Self::SettingAboveCeiling,
        Self::ScanInFlight,
    ];

    /// The enum value (`type` on the wire).
    #[must_use]
    pub fn value(self) -> &'static str {
        match self {
            Self::InvalidScreenDefinition => "invalid-screen-definition",
            Self::BadRequest => "bad-request",
            Self::Unauthenticated => "unauthenticated",
            Self::PaymentRequired => "payment-required",
            Self::NotFound => "not-found",
            Self::StaleDataVersion => "stale-data-version",
            Self::NoTradingDay => "no-trading-day",
            Self::RateLimited => "rate-limited",
            Self::PipelineDegraded => "pipeline-degraded",
            Self::InternalError => "internal-error",
            Self::SettingBelowFloor => "setting-below-floor",
            Self::UnderlyingNotAllowed => "underlying-not-allowed",
            Self::SettingAboveCeiling => "setting-above-ceiling",
            Self::ScanInFlight => "scan-in-flight",
        }
    }

    /// `STATUS_FOR`.
    #[must_use]
    pub fn status(self) -> u16 {
        match self {
            Self::InvalidScreenDefinition | Self::BadRequest => 400,
            Self::Unauthenticated => 401,
            Self::PaymentRequired => 402,
            Self::NotFound => 404,
            Self::StaleDataVersion | Self::ScanInFlight => 409,
            Self::NoTradingDay
            | Self::SettingBelowFloor
            | Self::UnderlyingNotAllowed
            | Self::SettingAboveCeiling => 422,
            Self::RateLimited => 429,
            Self::PipelineDegraded => 503,
            Self::InternalError => 500,
        }
    }

    /// `TITLE_FOR`.
    #[must_use]
    pub fn title(self) -> &'static str {
        match self {
            Self::InvalidScreenDefinition => "Invalid screen definition",
            Self::BadRequest => "Bad request",
            Self::Unauthenticated => "Authentication required",
            Self::PaymentRequired => "Your plan does not include this feature",
            Self::NotFound => "Not found",
            Self::StaleDataVersion => "Data version is no longer current",
            Self::NoTradingDay => "No trading day available for that date",
            Self::RateLimited => "Too many requests",
            Self::PipelineDegraded => "Data pipeline is degraded",
            Self::InternalError => "Internal server error",
            Self::SettingBelowFloor => "Setting is below the server's floor",
            Self::UnderlyingNotAllowed => "Underlying is not allowed",
            Self::SettingAboveCeiling => "Setting exceeds the server's ceiling",
            Self::ScanInFlight => "A scan is already in flight",
        }
    }

    /// `app._TYPE_FOR_STATUS`: the type a framework-raised status maps to, `internal-error`
    /// for any other (with the original status kept).
    #[must_use]
    pub fn for_framework_status(status: u16) -> Self {
        match status {
            401 => Self::Unauthenticated,
            402 => Self::PaymentRequired,
            404 => Self::NotFound,
            409 => Self::StaleDataVersion,
            422 => Self::NoTradingDay,
            429 => Self::RateLimited,
            503 => Self::PipelineDegraded,
            _ => Self::InternalError,
        }
    }
}

/// One RFC 9457 problem. Ports `Problem`.
#[derive(Debug, Clone, PartialEq)]
pub struct Problem {
    /// `type`.
    pub kind: ProblemType,
    /// `status` (normally `kind.status()`; a framework error keeps its own, e.g. 405).
    pub status: u16,
    /// `title`.
    pub title: String,
    /// `detail`.
    pub detail: String,
    /// Response headers (`WWW-Authenticate`, `Retry-After`), in insertion order.
    pub headers: Vec<(String, String)>,
    /// Extension members in insertion order; `Null` ones are omitted from the body.
    pub extra: Vec<(String, OrderedJson)>,
    /// Python's catch-all `Exception` handler runs in Starlette's `ServerErrorMiddleware`,
    /// outside every user middleware: such a response carries no `X-Request-Id` and no CORS
    /// headers. `true` reproduces that.
    pub outside_middleware: bool,
}

impl Problem {
    /// `Problem(problem_type, detail)`.
    #[must_use]
    pub fn new(kind: ProblemType, detail: impl Into<String>) -> Self {
        Self {
            kind,
            status: kind.status(),
            title: kind.title().to_owned(),
            detail: detail.into(),
            headers: Vec::new(),
            extra: Vec::new(),
            outside_middleware: false,
        }
    }

    /// Adds a response header.
    #[must_use]
    pub fn header(mut self, name: &str, value: impl Into<String>) -> Self {
        self.headers.push((name.to_owned(), value.into()));
        self
    }

    /// Adds an extension member.
    #[must_use]
    pub fn with(mut self, key: &str, value: OrderedJson) -> Self {
        self.extra.push((key.to_owned(), value));
        self
    }

    /// Ports `Problem.body(instance)`.
    #[must_use]
    pub fn body(&self, instance: Option<&str>) -> OrderedJson {
        let mut entries = vec![
            (
                "type".to_owned(),
                OrderedJson::Str(self.kind.value().to_owned()),
            ),
            ("title".to_owned(), OrderedJson::Str(self.title.clone())),
            (
                "status".to_owned(),
                OrderedJson::Int(i64::from(self.status)),
            ),
            ("detail".to_owned(), OrderedJson::Str(self.detail.clone())),
        ];
        if let Some(path) = instance {
            entries.push(("instance".to_owned(), OrderedJson::Str(path.to_owned())));
        }
        for (k, v) in &self.extra {
            if *v == OrderedJson::Null {
                continue;
            }
            if let Some(slot) = entries.iter_mut().find(|(key, _)| key == k) {
                slot.1 = v.clone();
            } else {
                entries.push((k.clone(), v.clone()));
            }
        }
        OrderedJson::Object(entries)
    }

    /// `app._problem_response`: the problem rendered for `instance` (the request path). The
    /// request id is added by the request-context layer, which sees every response but the
    /// [`Problem::outside_middleware`] ones.
    #[must_use]
    pub fn into_response_for(self, instance: &str) -> Response {
        let body = wire::to_json_bytes(&self.body(Some(instance)));
        let status = StatusCode::from_u16(self.status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
        let mut response = (status, body).into_response();
        let headers = response.headers_mut();
        headers.insert(
            axum::http::header::CONTENT_TYPE,
            HeaderValue::from_static(CONTENT_TYPE),
        );
        for (name, value) in &self.headers {
            if let (Ok(n), Ok(v)) = (
                HeaderName::from_bytes(name.as_bytes()),
                HeaderValue::from_str(value),
            ) {
                headers.insert(n, v);
            }
        }
        if self.outside_middleware {
            response.extensions_mut().insert(OutsideMiddleware);
        }
        response
    }
}

/// Marks a response the request-context and CORS layers must pass through untouched (see
/// [`Problem::outside_middleware`]).
#[derive(Debug, Clone, Copy)]
pub struct OutsideMiddleware;

/// A problem raised inside a handler; rendered with the request path by the app's
/// `problem_layer` (FastAPI's exception handlers see `request.url.path`).
impl IntoResponse for Problem {
    fn into_response(self) -> Response {
        let mut response = StatusCode::INTERNAL_SERVER_ERROR.into_response();
        response
            .extensions_mut()
            .insert(PendingProblem(Box::new(self)));
        response
    }
}

/// A [`Problem`] waiting for its `instance`. The app's outermost routing layer replaces any
/// response carrying one with [`Problem::into_response_for`] the request path.
#[derive(Debug, Clone)]
pub struct PendingProblem(pub Box<Problem>);

/// `unauthenticated(detail)`.
#[must_use]
pub fn unauthenticated(detail: &str) -> Problem {
    Problem::new(ProblemType::Unauthenticated, detail).header("WWW-Authenticate", "Bearer")
}

/// `unauthenticated()` with its default sentence.
#[must_use]
pub fn unauthenticated_default() -> Problem {
    unauthenticated("A valid bearer token is required.")
}

/// `not_found(what, identifier)`: `No {what} with id {identifier!r}.`
#[must_use]
pub fn not_found(what: &str, identifier: &str) -> Problem {
    Problem::new(
        ProblemType::NotFound,
        format!(
            "No {what} with id {}.",
            baskfy_providers::py::repr_str(identifier)
        ),
    )
}

/// `payment_required(feature, detail=None)`.
#[must_use]
pub fn payment_required(feature: &str, detail: Option<&str>) -> Problem {
    let detail = detail.map_or_else(
        || {
            format!(
                "{} is not included in your plan.",
                baskfy_providers::py::repr_str(feature)
            )
        },
        str::to_owned,
    );
    Problem::new(ProblemType::PaymentRequired, detail)
        .with("feature", OrderedJson::Str(feature.to_owned()))
        .with("upgrade_url", OrderedJson::Str(UPGRADE_URL.to_owned()))
}

/// `rate_limited(retry_after_seconds, limit_per_minute)`.
#[must_use]
pub fn rate_limited(retry_after_seconds: i64, limit_per_minute: i64) -> Problem {
    Problem::new(
        ProblemType::RateLimited,
        format!("Rate limit of {limit_per_minute} requests per minute exceeded."),
    )
    .header("Retry-After", retry_after_seconds.to_string())
    .with("retry_after", OrderedJson::Int(retry_after_seconds))
    .with("limit_per_minute", OrderedJson::Int(limit_per_minute))
}

/// `stale_data_version(sent, current)`.
#[must_use]
pub fn stale_data_version(sent: i64, current: i64) -> Problem {
    Problem::new(
        ProblemType::StaleDataVersion,
        format!("data_version {sent} is not the current one ({current}); re-fetch and retry."),
    )
    .with("sent_data_version", OrderedJson::Int(sent))
    .with("current_data_version", OrderedJson::Int(current))
}

/// `no_trading_day(requested, earliest, latest)`.
#[must_use]
pub fn no_trading_day(
    requested: chrono::NaiveDate,
    earliest: chrono::NaiveDate,
    latest: chrono::NaiveDate,
) -> Problem {
    let iso = |d: chrono::NaiveDate| OrderedJson::Str(wire::py_date(d));
    Problem::new(
        ProblemType::NoTradingDay,
        format!(
            "{} is outside the servable range.",
            wire::py_date(requested)
        ),
    )
    .with("requested", iso(requested))
    .with("earliest_available", iso(earliest))
    .with("latest_available", iso(latest))
}

/// One entry of `errors[]`: Python passes dicts, so the keys are whatever the caller gives, in
/// the caller's order.
pub type ErrorEntry = Vec<(String, OrderedJson)>;

fn errors_value(errors: Vec<ErrorEntry>) -> OrderedJson {
    OrderedJson::Array(errors.into_iter().map(OrderedJson::Object).collect())
}

/// `invalid_screen_definition(errors)`.
#[must_use]
pub fn invalid_screen_definition(errors: Vec<ErrorEntry>) -> Problem {
    Problem::new(
        ProblemType::InvalidScreenDefinition,
        "The screen definition failed validation.",
    )
    .with("errors", errors_value(errors))
}

/// `bad_request(detail, errors=None)`.
#[must_use]
pub fn bad_request(detail: &str, errors: Option<Vec<ErrorEntry>>) -> Problem {
    let problem = Problem::new(ProblemType::BadRequest, detail);
    match errors {
        Some(e) => problem.with("errors", errors_value(e)),
        None => problem,
    }
}

/// `pipeline_degraded(detail)`.
#[must_use]
pub fn pipeline_degraded(detail: &str) -> Problem {
    Problem::new(ProblemType::PipelineDegraded, detail)
}

/// `setting_above_ceiling(field=, value=, ceiling=, env_var=)`; `value` and `ceiling` are the
/// Python `str()` of each.
#[must_use]
pub fn setting_above_ceiling(field: &str, value: &str, ceiling: &str, env_var: &str) -> Problem {
    Problem::new(
        ProblemType::SettingAboveCeiling,
        format!(
            "{field} may not exceed {ceiling}; {value} was requested. This ceiling is server \
             configuration ({env_var}) and is not editable here."
        ),
    )
    .with("field", OrderedJson::Str(field.to_owned()))
    .with("requested", OrderedJson::Str(value.to_owned()))
    .with("ceiling", OrderedJson::Str(ceiling.to_owned()))
    .with("env_var", OrderedJson::Str(env_var.to_owned()))
}

/// `setting_below_floor(field=, value=, floor=, env_var=)`.
#[must_use]
pub fn setting_below_floor(field: &str, value: &str, floor: &str, env_var: &str) -> Problem {
    Problem::new(
        ProblemType::SettingBelowFloor,
        format!(
            "{field} may not be below {floor}; {value} was requested. This floor is server \
             configuration ({env_var}) and is not editable here."
        ),
    )
    .with("field", OrderedJson::Str(field.to_owned()))
    .with("requested", OrderedJson::Str(value.to_owned()))
    .with("floor", OrderedJson::Str(floor.to_owned()))
    .with("env_var", OrderedJson::Str(env_var.to_owned()))
}

/// `underlying_not_allowed(value=, allowed=)`; `value_repr` is Python's `repr(value)`.
#[must_use]
pub fn underlying_not_allowed(value_repr: &str, value_str: &str, allowed: &[&str]) -> Problem {
    Problem::new(
        ProblemType::UnderlyingNotAllowed,
        format!(
            "underlying {value_repr} is not allowed; the options book trades {} only in v1 \
             (docs/options/02 Track C §5).",
            allowed.join(", ")
        ),
    )
    .with("field", OrderedJson::Str("underlying".to_owned()))
    .with("requested", OrderedJson::Str(value_str.to_owned()))
    .with(
        "allowed",
        OrderedJson::Array(
            allowed
                .iter()
                .map(|a| OrderedJson::Str((*a).to_owned()))
                .collect(),
        ),
    )
}

/// The `Exception` catch-all: `An unexpected error occurred.`, with no request id and no CORS
/// headers (see [`Problem::outside_middleware`]). The cause is logged, never sent.
#[must_use]
pub fn internal_error() -> Problem {
    let mut p = Problem::new(ProblemType::InternalError, "An unexpected error occurred.");
    p.outside_middleware = true;
    p
}

/// Starlette's own `HTTPException` (an unrouted path, a bad method) through `_handle_http`.
#[must_use]
pub fn framework(status: u16, detail: &str) -> Problem {
    let kind = ProblemType::for_framework_status(status);
    let mut p = Problem::new(kind, detail);
    p.status = status;
    p
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bad_request_is_a_400_distinct_from_invalid_screen() {
        // Ports test_bad_request_problem.py.
        assert_eq!(ProblemType::BadRequest.value(), "bad-request");
        assert_eq!(ProblemType::BadRequest.status(), 400);
        let p = bad_request("nope", None);
        assert_eq!(p.kind, ProblemType::BadRequest);
        assert_eq!(p.status, 400);
        assert_ne!(p.kind, ProblemType::InvalidScreenDefinition);
    }

    #[test]
    fn the_body_is_pythons_dict_in_insertion_order() {
        let body = rate_limited(7, 10)
            .body(Some("/api/v1/x"))
            .to_python_json()
            .unwrap_or_default();
        assert_eq!(
            body,
            r#"{"type":"rate-limited","title":"Too many requests","status":429,"detail":"Rate limit of 10 requests per minute exceeded.","instance":"/api/v1/x","retry_after":7,"limit_per_minute":10}"#
        );
        let p = payment_required("export_csv", None);
        assert_eq!(p.detail, "'export_csv' is not included in your plan.");
        assert_eq!(
            not_found("screen", "ab'c").detail,
            r#"No screen with id "ab'c"."#
        );
        // A None extension member is omitted.
        let p = Problem::new(ProblemType::NotFound, "x").with("gone", OrderedJson::Null);
        assert!(
            !p.body(None)
                .to_python_json()
                .unwrap_or_default()
                .contains("gone")
        );
    }

    #[test]
    fn every_type_has_its_status_and_title() {
        for t in ProblemType::ALL {
            assert!(!t.title().is_empty());
            assert!((400..=503).contains(&t.status()));
        }
        assert_eq!(
            ProblemType::for_framework_status(405),
            ProblemType::InternalError
        );
        assert_eq!(framework(405, "Method Not Allowed").status, 405);
    }
}
