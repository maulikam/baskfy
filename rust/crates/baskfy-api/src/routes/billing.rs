//! ``/plans`` and ``/invoices`` — docs/07 §"Account & billing". Ports the GET routes of
//! `baskfy_api.routers.billing` (Python declares `APIRouter(tags=["billing"])`).
//!
//! Ported: `GET /plans` (public: `/pricing` renders signed out), `GET /invoices` (cursor-paginated
//! on the payment id) and `GET /invoices/{invoice_number:path}/pdf` (streamed from the invoice
//! archive, never a presigned URL). **Not ported** (write routes, Python keeps serving them):
//! `POST /checkout/session`, `POST /webhooks/razorpay`.
//!
//! The PDF route's `{invoice_number:path}` is Starlette's `.*`; axum has no mid-path catch-all,
//! so the route is registered for invoice numbers of one to [`MAX_INVOICE_SEGMENTS`] segments
//! (the real series is three: `DCL/2026-27/000001`). DECISIONS-RS, api-portfolio P4.

use axum::Router;
use axum::extract::{Extension, Path, RawQuery, State};
use axum::http::{HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use baskfy_core::decimal::WideDecimal;
use baskfy_db::OrderedJson;
use chrono::{DateTime, NaiveDate, Utc};
use serde::Serialize;

use crate::auth::{Principal, require_authenticated};
use crate::entitlements::{Entitlements, EntitlementsOut};
use crate::problems::{Problem, ProblemType, not_found};
use crate::query::{QueryParams, Validation};
use crate::routes::portfolios::portfolio::{db, num, params, pyfmt};
use crate::settings::API_PREFIX;
use crate::state::AppState;
use crate::wire;

/// `DEFAULT_PAGE_SIZE`.
pub const DEFAULT_PAGE_SIZE: i128 = 100;
/// `MAX_PAGE_SIZE`.
pub const MAX_PAGE_SIZE: i128 = 500;
/// The deepest invoice number (in `/` segments) the PDF route is registered for.
pub const MAX_INVOICE_SEGMENTS: usize = 8;
/// `FREE_PLAN.code`.
pub const FREE_PLAN_CODE: &str = "free";

/// `PRE_PURCHASE_DISCLAIMERS` (`baskfy_core.seed_data`), verbatim.
pub const PRE_PURCHASE_DISCLAIMERS: [&str; 5] = [
    "Baskfy is not a SEBI-registered investment adviser. Nothing here is investment advice, a \
     recommendation to buy or sell, or a target price.",
    "Baskfy is a screening and analytics tool. Every number on it is derived from historical \
     exchange data and can be wrong, stale, or both.",
    "Past performance — including anything a backtest shows — does not indicate future returns.",
    "Prices are in Indian rupees and include GST at the prevailing rate. A GST invoice is issued \
     for every payment and is available under Invoices.",
    "Subscriptions renew automatically until cancelled. Cancel any time; access continues to the \
     end of the period already paid for.",
];

/// `PlanFeatureOut`.
#[derive(Debug, Clone, Serialize)]
pub struct PlanFeatureOut {
    /// `label`.
    pub label: String,
    /// `entitlement`.
    pub entitlement: Option<String>,
}

/// `PlanOut`.
#[derive(Debug, Clone, Serialize)]
pub struct PlanOut {
    /// `code`.
    pub code: String,
    /// `label`.
    pub label: String,
    /// `tagline`.
    pub tagline: String,
    /// `price_inr`.
    #[serde(serialize_with = "num::ser")]
    pub price_inr: WideDecimal,
    /// `currency` (always `INR`).
    pub currency: &'static str,
    /// `interval`: `month` | `year` | null.
    pub interval: Option<String>,
    /// `features`.
    pub features: Vec<PlanFeatureOut>,
    /// `entitlements`.
    pub entitlements: EntitlementsOut,
    /// `price_from_dec_2026`.
    #[serde(serialize_with = "num::ser_opt")]
    pub price_from_dec_2026: Option<WideDecimal>,
    /// `disclosure`.
    pub disclosure: Option<String>,
}

/// `PlanListOut`.
#[derive(Debug, Clone, Serialize)]
pub struct PlanListOut {
    /// `data`.
    pub data: Vec<PlanOut>,
    /// `disclaimers`.
    pub disclaimers: Vec<&'static str>,
    /// `free_tier_enabled`.
    pub free_tier_enabled: bool,
}

/// `InvoiceOut`.
#[derive(Debug, Clone, Serialize)]
pub struct InvoiceOut {
    /// `invoice_number`.
    pub invoice_number: String,
    /// `invoice_date`.
    #[serde(serialize_with = "wire::ser::date")]
    pub invoice_date: NaiveDate,
    /// `plan_code`.
    pub plan_code: Option<String>,
    /// `amount_inr`.
    #[serde(serialize_with = "num::ser")]
    pub amount_inr: WideDecimal,
    /// `taxable_inr`.
    #[serde(serialize_with = "num::ser_opt")]
    pub taxable_inr: Option<WideDecimal>,
    /// `cgst_inr`.
    #[serde(serialize_with = "num::ser_opt")]
    pub cgst_inr: Option<WideDecimal>,
    /// `sgst_inr`.
    #[serde(serialize_with = "num::ser_opt")]
    pub sgst_inr: Option<WideDecimal>,
    /// `igst_inr`.
    #[serde(serialize_with = "num::ser_opt")]
    pub igst_inr: Option<WideDecimal>,
    /// `gst_inr`.
    #[serde(serialize_with = "num::ser_opt")]
    pub gst_inr: Option<WideDecimal>,
    /// `gst_rate`.
    #[serde(serialize_with = "num::ser_opt")]
    pub gst_rate: Option<WideDecimal>,
    /// `place_of_supply`.
    pub place_of_supply: Option<String>,
    /// `sac_code`.
    pub sac_code: Option<String>,
    /// `status`.
    pub status: String,
    /// `pdf_url`.
    pub pdf_url: String,
}

/// `InvoicePage`.
#[derive(Debug, Clone, Serialize)]
pub struct InvoicePage {
    /// `data`.
    pub data: Vec<InvoiceOut>,
    /// `next_cursor`.
    pub next_cursor: Option<String>,
}

fn plan_out(
    code: String,
    price_inr: WideDecimal,
    interval: Option<String>,
    features: &OrderedJson,
) -> Result<PlanOut, Problem> {
    let includes = match features.get("includes") {
        Some(OrderedJson::Array(items)) => items.as_slice(),
        _ => &[],
    };
    let entitlements = Entitlements::from_plan_features(features);
    let label = match features.get("label") {
        Some(v) if pyfmt::truthy(v) => pyfmt::py_str(v),
        _ => pyfmt::py_title(&code.replace('_', " ")),
    };
    let tagline = match features.get("tagline") {
        Some(v) if pyfmt::truthy(v) => pyfmt::py_str(v),
        _ => String::new(),
    };
    let price_from_dec_2026 = match features.get("price_from_dec_2026") {
        Some(OrderedJson::Str(s)) => Some(s.parse::<WideDecimal>().map_err(|e| {
            crate::logging::error(
                "baskfy_api.app",
                "unhandled error",
                &[("error", &e.to_string())],
            );
            crate::problems::internal_error()
        })?),
        _ => None,
    };
    let disclosure = match features.get("disclosure") {
        Some(OrderedJson::Str(s)) => Some(s.clone()),
        _ => None,
    };
    let feature_rows = includes
        .iter()
        .filter(|entry| matches!(entry, OrderedJson::Object(_)))
        .map(|entry| PlanFeatureOut {
            label: entry.get("label").map_or_else(String::new, pyfmt::py_str),
            entitlement: entry
                .get("entitlement")
                .filter(|v| pyfmt::truthy(v))
                .map(pyfmt::py_str),
        })
        .collect();
    Ok(PlanOut {
        code,
        label,
        tagline,
        price_inr,
        currency: "INR",
        interval: interval.filter(|i| i == "month" || i == "year"),
        features: feature_rows,
        entitlements: entitlements.as_out(),
        price_from_dec_2026,
        disclosure,
    })
}

/// `GET /plans`: the catalogue in `plan.id` order; the ₹0 tier only when its flag is on.
///
/// # Errors
/// 500 for a database failure.
pub async fn list_plans(State(state): State<AppState>) -> Result<Response, Problem> {
    let rows = sqlx::query("SELECT code, price_inr, interval, features FROM plan ORDER BY id")
        .fetch_all(state.db.pool())
        .await
        .map_err(db::fail)?;
    let free_tier_enabled = state.settings.free_tier_enabled;
    let mut data = Vec::new();
    for row in &rows {
        let code: String = db::get(row, "code")?;
        if code == FREE_PLAN_CODE && !free_tier_enabled {
            continue;
        }
        let features = db::opt_json(row, "features")?.unwrap_or(OrderedJson::Object(Vec::new()));
        data.push(plan_out(
            code,
            db::dec(row, "price_inr")?,
            db::get(row, "interval")?,
            &features,
        )?);
    }
    Ok(wire::json(&PlanListOut {
        data,
        disclaimers: PRE_PURCHASE_DISCLAIMERS.to_vec(),
        free_tier_enabled,
    }))
}

fn invalid_cursor() -> Problem {
    Problem::new(
        ProblemType::InvalidScreenDefinition,
        "That cursor is not a valid one.",
    )
    .with(
        "errors",
        OrderedJson::Array(vec![OrderedJson::Object(vec![
            ("field".to_owned(), OrderedJson::Str("cursor".to_owned())),
            (
                "message".to_owned(),
                OrderedJson::Str("expected an integer".to_owned()),
            ),
        ])]),
    )
}

/// `GET /invoices?limit=&cursor=`: the caller's invoiced payments, newest first.
///
/// # Errors
/// 401 anonymous; 400 for a bad `limit` or a cursor that is not an integer; 500 for a database
/// failure (including a cursor beyond `bigint`).
pub async fn list_invoices(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let q = QueryParams::parse(raw.as_deref());
    let mut v = Validation::new();
    let limit = params::query_int(
        &mut v,
        &q,
        "limit",
        DEFAULT_PAGE_SIZE,
        params::IntBounds {
            ge: Some(1),
            le: Some(MAX_PAGE_SIZE),
            gt: None,
        },
    );
    let cursor = q.get("cursor").map(str::to_owned);
    v.finish()?;
    let limit = limit.unwrap_or(DEFAULT_PAGE_SIZE);
    let user_id = principal.require_user()?;
    let cursor_id = match cursor.as_deref() {
        Some(c) => Some(params::bigint(
            params::python_int(c).ok_or_else(invalid_cursor)?,
        )?),
        None => None,
    };
    let fetch = i64::try_from(limit + 1).unwrap_or(i64::MAX);
    let rows = sqlx::query(
        "SELECT p.id, p.invoice_number, p.invoice_date, p.created_at, p.amount_inr, \
         p.taxable_inr, p.cgst_inr, p.sgst_inr, p.igst_inr, p.gst_inr, p.gst_rate, \
         p.place_of_supply, p.sac_code, p.status, plan.code AS plan_code \
         FROM payment p \
         LEFT OUTER JOIN subscription s ON s.id = p.subscription_id \
         LEFT OUTER JOIN plan ON plan.id = s.plan_id \
         WHERE p.user_id = $1 AND p.invoice_number IS NOT NULL \
         AND ($2::bigint IS NULL OR p.id < $2) \
         ORDER BY p.id DESC LIMIT $3",
    )
    .bind(user_id)
    .bind(cursor_id)
    .bind(fetch)
    .fetch_all(state.db.pool())
    .await
    .map_err(db::fail)?;
    let page_len = usize::try_from(limit).unwrap_or(usize::MAX).min(rows.len());
    let mut data = Vec::with_capacity(page_len);
    let mut last_id: Option<i64> = None;
    for row in &rows[..page_len] {
        let number: Option<String> = db::get(row, "invoice_number")?;
        let number = number.unwrap_or_default();
        let created_at: DateTime<Utc> = db::ts(row, "created_at")?;
        last_id = Some(db::get(row, "id")?);
        data.push(InvoiceOut {
            invoice_date: db::opt_date(row, "invoice_date")?.unwrap_or(created_at.date_naive()),
            plan_code: db::get(row, "plan_code")?,
            amount_inr: db::dec(row, "amount_inr")?,
            taxable_inr: db::opt_dec(row, "taxable_inr")?,
            cgst_inr: db::opt_dec(row, "cgst_inr")?,
            sgst_inr: db::opt_dec(row, "sgst_inr")?,
            igst_inr: db::opt_dec(row, "igst_inr")?,
            gst_inr: db::opt_dec(row, "gst_inr")?,
            gst_rate: db::opt_dec(row, "gst_rate")?,
            place_of_supply: db::get(row, "place_of_supply")?,
            sac_code: db::get(row, "sac_code")?,
            status: db::get(row, "status")?,
            pdf_url: format!("{API_PREFIX}/invoices/{number}/pdf"),
            invoice_number: number,
        });
    }
    let next_cursor = if rows.len() > page_len && page_len > 0 {
        last_id.map(|id| id.to_string())
    } else {
        None
    };
    Ok(wire::json(&InvoicePage { data, next_cursor }))
}

/// `GET /invoices/{invoice_number:path}/pdf`: the caller's own invoice PDF, streamed.
///
/// # Errors
/// 401 anonymous; 404 when the invoice is not the caller's or has no PDF (not-yours reads as
/// absent); 500 for a database or archive failure.
pub async fn download_invoice(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    Path(segments): Path<Vec<(String, String)>>,
) -> Result<Response, Problem> {
    let invoice_number = segments
        .iter()
        .map(|(_, v)| v.as_str())
        .collect::<Vec<_>>()
        .join("/");
    // Starlette's `.*` is a Python regex `.`, which never matches a newline: a decoded `%0A`
    // anywhere in the number means Python routed nothing, so its answer is the unrouted 404,
    // before any dependency (the token included) runs.
    if invoice_number.contains('\n') {
        return Err(crate::problems::framework(404, "Not Found"));
    }
    let principal = require_authenticated(&principal)?;
    let user_id = principal.require_user()?;
    let key: Option<Option<String>> = sqlx::query_scalar(
        "SELECT invoice_pdf_key FROM payment WHERE user_id = $1 AND invoice_number = $2",
    )
    .bind(user_id)
    .bind(&invoice_number)
    .fetch_optional(state.db.pool())
    .await
    .map_err(db::fail)?;
    let Some(Some(key)) = key else {
        return Err(not_found("invoice", &invoice_number));
    };
    let unhandled = |e: &dyn std::fmt::Display| {
        crate::logging::error(
            "baskfy_api.app",
            "unhandled error",
            &[("error", &e.to_string())],
        );
        crate::problems::internal_error()
    };
    let provider_settings =
        baskfy_providers::settings::ProviderSettings::from_env().map_err(|e| unhandled(&e))?;
    let archive = baskfy_providers::factory::build_archive(
        &provider_settings,
        Some(std::path::Path::new(&state.settings.invoice_local_dir)),
    )
    .map_err(|e| unhandled(&e))?;
    let payload = archive.get(&key).await.map_err(|e| unhandled(&e))?;
    let filename = format!("{}.pdf", invoice_number.replace('/', "-"));
    let mut response = (StatusCode::OK, payload).into_response();
    let headers = response.headers_mut();
    headers.insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("application/pdf"),
    );
    if let Ok(v) = HeaderValue::from_str(&format!("attachment; filename=\"{filename}\"")) {
        headers.insert(header::CONTENT_DISPOSITION, v);
    }
    Ok(response)
}

/// The GET routes of `baskfy_api.routers.billing`, relative to `/api/v1`.
pub fn router() -> Router<AppState> {
    let mut router = Router::new()
        .route("/plans", get(list_plans))
        .route("/invoices", get(list_invoices))
        // The write routes' paths, with no method: a GET there is Starlette's 405.
        .route("/checkout/session", axum::routing::MethodRouter::new())
        .route("/webhooks/razorpay", axum::routing::MethodRouter::new());
    for depth in 1..=MAX_INVOICE_SEGMENTS {
        let segments: Vec<String> = (0..depth).map(|i| format!("{{s{i}}}")).collect();
        let path = format!("/invoices/{}/pdf", segments.join("/"));
        router = router.route(&path, get(download_invoice));
    }
    router
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_plan_renders_from_its_row_alone() {
        // Ports test_api_billing.py's plan-rendering assertions on a bare feature map.
        let features = OrderedJson::parse(
            r#"{"label": "Monthly", "includes": [{"label": "Backtests", "entitlement": "backtests"},
            {"label": "Community Slack access", "entitlement": null}, "not-a-dict"],
            "entitlements": {"screener": true, "max_screens": 50}, "price_from_dec_2026": "899.00"}"#,
        )
        .unwrap_or(OrderedJson::Null);
        let Ok(plan) = plan_out(
            "monthly".to_owned(),
            "500.00".parse().unwrap_or_default(),
            Some("month".to_owned()),
            &features,
        ) else {
            unreachable!("a valid plan")
        };
        let body = String::from_utf8(wire::to_json_bytes(&plan)).unwrap_or_default();
        assert!(body.starts_with(r#"{"code":"monthly","label":"Monthly","tagline":"","price_inr":"500.00","currency":"INR","interval":"month","features":[{"label":"Backtests","entitlement":"backtests"},{"label":"Community Slack access","entitlement":null}]"#), "{body}");
        assert!(
            body.ends_with(r#""price_from_dec_2026":"899.00","disclosure":null}"#),
            "{body}"
        );
        let Ok(plain) = plan_out(
            "pro_plan".to_owned(),
            "1".parse().unwrap_or_default(),
            None,
            &OrderedJson::Object(vec![]),
        ) else {
            unreachable!("a valid plan")
        };
        assert_eq!(plain.label, "Pro Plan");
        assert!(plain.interval.is_none());
    }
}
