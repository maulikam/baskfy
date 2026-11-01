//! ``POST /cb/baskets/from-screen`` and ``POST /cb/baskets/{slug}/recut`` — a screen saved as an
//! investable basket (SB1), sized by `baskfy_core::basket_sizing`. The server runs the screen
//! itself. **No route may place, modify or cancel an order** (law 2).

use std::collections::BTreeMap;
use std::sync::LazyLock;

use axum::Router;
use axum::extract::{Extension, Path, Request, State};
use axum::http::StatusCode;
use axum::response::Response;
use axum::routing::post;
use baskfy_core::basket_sizing::{
    self, Candidate, HoldingProfile, MAX_HOLDINGS, MIN_HOLDINGS, SizedBasket, WeightMethod,
};
use baskfy_core::decimal::WideDecimal;
use baskfy_core::screener::{Payload, PyDecimal, ScreenDefinition};
use baskfy_db::OrderedJson;
use chrono::NaiveDate;
use regex::Regex;
use sqlx::{PgConnection, Row as _};

use super::curated_create::writes::{curated_manager_id, slug_for};
use super::screens::accounts::scoped_sole_user_id;
use super::screens::body_model::{self, Errors, Model, Ty, V, opt, req};
use super::screens::write_kit::{self, db, numeric_text};
use super::screens::{stored_definition, visible_to};
use crate::auth::{Principal, require_authenticated};
use crate::entitlements::entitlements_for;
use crate::problems::{Problem, ProblemType, internal_error, not_found};
use crate::screener::{self, NoCacheError};
use crate::session::{self, Tx, WritePool};
use crate::state::AppState;
use crate::wire;

const PRICE_COLUMN: &str = "close_raw";
const VOL_COLUMN: &str = "vol_12m";
const SCORE_COLUMN: &str = "sorting_factor";
const PROFILES: &[&str] = &["CONSERVATIVE", "BALANCED", "AGGRESSIVE"];
const METHODS: &[&str] = &["EQUAL", "RANK", "SCORE", "INV_VOL", "CUSTOM"];

static METHOD_IN_NOTES: LazyLock<Option<Regex>> =
    LazyLock::new(|| Regex::new(r"\b(EQUAL|RANK|SCORE|INV_VOL|CUSTOM) weights\.").ok());

fn opt_int_between(lo: i128, hi: i128) -> Ty {
    Ty::Opt(Box::new(Ty::Int {
        gt: None,
        ge: Some(lo),
        le: Some(hi),
    }))
}

fn from_screen_in() -> Model {
    Model {
        fields: vec![
            req("screen_public_id", Ty::Str(Some(1), Some(64))),
            req(
                "amount",
                Ty::Dec {
                    gt: Some("0"),
                    ge: None,
                    le: None,
                },
            ),
            opt("name", Ty::Opt(Box::new(Ty::Str(None, Some(120))))),
            opt("description_md", Ty::Opt(Box::new(Ty::Str(None, None)))),
            opt("profile", Ty::Opt(Box::new(Ty::Enum(PROFILES)))),
            opt(
                "holdings",
                opt_int_between(i128::from(MIN_HOLDINGS), i128::from(MAX_HOLDINGS)),
            ),
            opt("exposure_tier", Ty::Opt(Box::new(Ty::Str(None, Some(8))))),
            opt(
                "cash_pct",
                Ty::Opt(Box::new(Ty::Dec {
                    gt: None,
                    ge: Some("0"),
                    le: Some("95"),
                })),
            ),
            opt("method", Ty::Opt(Box::new(Ty::Enum(METHODS)))),
            opt(
                "custom_weights",
                Ty::Opt(Box::new(Ty::List(
                    Box::new(Ty::Model(Model {
                        fields: vec![
                            req("symbol", Ty::Str(Some(1), Some(32))),
                            req(
                                "weight",
                                Ty::Dec {
                                    gt: Some("0"),
                                    ge: None,
                                    le: None,
                                },
                            ),
                        ],
                        forbid_extra: false,
                    })),
                    None,
                ))),
            ),
        ],
        forbid_extra: false,
    }
}

fn recut_in() -> Model {
    Model {
        fields: vec![
            opt("confirm", Ty::Bool),
            opt(
                "holdings",
                opt_int_between(i128::from(MIN_HOLDINGS), i128::from(MAX_HOLDINGS)),
            ),
            opt("method", Ty::Opt(Box::new(Ty::Enum(METHODS)))),
        ],
        forbid_extra: false,
    }
}

fn invalid(message: impl Into<String>) -> Problem {
    Problem::new(ProblemType::InvalidScreenDefinition, message)
}

/// `_price(row_values)`: a positive `Decimal(str(value))`, or `None`.
fn price(value: Option<&Payload>) -> Option<WideDecimal> {
    let text = match value? {
        Payload::Decimal(PyDecimal::Finite(d)) => d.py_str(),
        Payload::Int(i) => i.to_string(),
        Payload::Float(f) => baskfy_core::py::float_repr(*f),
        Payload::Str(s) => s.trim().to_owned(),
        _ => return None,
    };
    let d: WideDecimal = text.parse().ok()?;
    (!(d.is_zero() || d.is_sign_negative())).then_some(d)
}

/// The sizing inputs that are not the screen.
struct SizeArgs {
    amount: WideDecimal,
    profile: Option<HoldingProfile>,
    holdings: Option<i64>,
    exposure_tier: Option<String>,
    cash_pct: Option<WideDecimal>,
    method: Option<WeightMethod>,
    custom: Option<Vec<(String, WideDecimal)>>,
}

/// `_custom_map(entries)`: duplicates are a 400.
fn custom_map(entries: Option<&[V]>) -> Result<Option<Vec<(String, WideDecimal)>>, Problem> {
    let Some(entries) = entries else {
        return Ok(None);
    };
    let mut out: Vec<(String, WideDecimal)> = Vec::new();
    for entry in entries {
        let symbol = entry
            .get("symbol")
            .str()
            .unwrap_or_default()
            .trim_matches(crate::routes::common::py_space)
            .to_uppercase();
        if out.iter().any(|(k, _)| *k == symbol) {
            return Err(invalid(format!(
                "custom weights name {symbol} more than once"
            )));
        }
        out.push((
            symbol,
            entry.get("weight").dec().cloned().unwrap_or_default(),
        ));
    }
    Ok(Some(out))
}

fn run_problem(e: &screener::RunError<NoCacheError>) -> Problem {
    screener::problem(e)
}

/// `_sized(session, definition, body, default_holdings=)`.
async fn sized(
    conn: &mut PgConnection,
    definition: &ScreenDefinition,
    args: &SizeArgs,
    default_holdings: Option<i64>,
) -> Result<(SizedBasket, NaiveDate), Problem> {
    let resolution = screener::resolve_as_of::<NoCacheError>(conn, definition.historical_date)
        .await
        .map_err(|e| run_problem(&e))?;
    let data_version = screener::current_data_version(conn).await.map_err(db)?;
    let result = screener::execute_screen::<NoCacheError>(
        conn,
        definition,
        resolution.as_of,
        data_version,
        &[],
        resolution.requested,
        MAX_HOLDINGS,
    )
    .await
    .map_err(|e| run_problem(&e))?;
    fn get<'a>(row: &'a baskfy_core::screener::ScreenResultRow, k: &str) -> Option<&'a Payload> {
        row.values.iter().find(|(n, _)| n == k).map(|(_, v)| v)
    }
    let score_key = result.sorting_factor.key.clone();
    let mut candidates = Vec::with_capacity(result.rows.len());
    for row in &result.rows {
        let symbol = row.symbol().map_err(|_| internal_error())?;
        candidates.push(Candidate {
            symbol,
            rank: row.rank,
            price: price(get(row, PRICE_COLUMN)),
            score: price(get(row, SCORE_COLUMN).or_else(|| get(row, &score_key))),
            vol: price(get(row, VOL_COLUMN)),
        });
    }
    let cash = match &args.cash_pct {
        Some(c) => c.clone(),
        None => basket_sizing::cash_pct_for_tier(args.exposure_tier.as_deref()),
    };
    let holdings = match (args.holdings, default_holdings) {
        (Some(h), _) => Some(h),
        (None, None) => None,
        (None, Some(d)) => {
            Some(MIN_HOLDINGS.max(d.min(i64::try_from(candidates.len()).unwrap_or(i64::MAX))))
        }
    };
    let basket = basket_sizing::size_basket(
        &candidates,
        &args.amount,
        args.profile,
        holdings,
        Some(&cash),
        args.method,
        args.custom.as_deref(),
    )
    .map_err(invalid)?;
    Ok((basket, result.as_of))
}

/// `_instruments_for(session, symbols)`: upper-case symbol → active instrument id.
async fn instruments_for(
    tx: &mut Tx,
    symbols: &[String],
) -> Result<BTreeMap<String, i64>, Problem> {
    let rows = sqlx::query(
        "SELECT id, symbol FROM instrument WHERE upper(symbol) = ANY($1) AND is_active IS true",
    )
    .bind(symbols)
    .fetch_all(&mut **tx)
    .await
    .map_err(db)?;
    let mut by_symbol = BTreeMap::new();
    for r in &rows {
        let s: String = r.try_get("symbol").map_err(db)?;
        by_symbol.insert(s.to_uppercase(), r.try_get::<i64, _>("id").map_err(db)?);
    }
    let missing: Vec<&str> = symbols
        .iter()
        .filter(|s| !by_symbol.contains_key(*s))
        .map(String::as_str)
        .collect();
    if !missing.is_empty() {
        return Err(Problem::new(
            ProblemType::InternalError,
            format!(
                "the screen ranked symbols with no active instrument row: {}",
                missing.join(", ")
            ),
        ));
    }
    Ok(by_symbol)
}

async fn insert_constituents(
    tx: &mut Tx,
    version_id: i64,
    rows: &[(i64, String)],
) -> Result<(), Problem> {
    for page in rows.chunks(write_kit::INSERT_PAGE_ROWS) {
        let values = write_kit::values_rows(page.len(), 2, |p| {
            format!("($1, ${}, 'Equity', (${}::text)::numeric)", p[0], p[1])
        });
        let sql = format!(
            "INSERT INTO cb_constituent (version_id, instrument_id, segment, weight) VALUES {values}"
        );
        let mut q = sqlx::query(&sql).bind(version_id);
        for (instrument_id, weight) in page {
            q = q.bind(*instrument_id).bind(weight);
        }
        q.execute(&mut **tx).await.map_err(db)?;
    }
    Ok(())
}

/// A `screen` row as these routes need it.
struct ScreenRef {
    id: i64,
    public_id: String,
    name: String,
    user_id: Option<i64>,
    definition: OrderedJson,
}

async fn screen_where(
    tx: &mut Tx,
    clause: &str,
    bind: ScreenKey<'_>,
) -> Result<Option<ScreenRef>, Problem> {
    let sql = format!("SELECT id, public_id, name, user_id, definition FROM screen WHERE {clause}");
    let q = sqlx::query(&sql);
    let q = match bind {
        ScreenKey::Public(p) => q.bind(p.to_owned()),
        ScreenKey::Id(i) => q.bind(i),
    };
    let Some(r) = q.fetch_optional(&mut **tx).await.map_err(db)? else {
        return Ok(None);
    };
    Ok(Some(ScreenRef {
        id: r.try_get("id").map_err(db)?,
        public_id: r.try_get("public_id").map_err(db)?,
        name: r.try_get("name").map_err(db)?,
        user_id: r.try_get("user_id").map_err(db)?,
        definition: baskfy_db::row::jsonb(&r, "definition").map_err(|e| crate::db_failure(&e))?,
    }))
}

enum ScreenKey<'a> {
    Public(&'a str),
    Id(i64),
}

fn dec(d: &WideDecimal) -> OrderedJson {
    OrderedJson::Str(d.to_sci_string())
}

fn obj(entries: Vec<(&str, OrderedJson)>) -> OrderedJson {
    OrderedJson::Object(
        entries
            .into_iter()
            .map(|(k, v)| (k.to_owned(), v))
            .collect(),
    )
}

/// `POST /cb/baskets/from-screen` (`create_basket_from_screen`): 201.
///
/// # Errors
/// 400, 401, 402, 404, 422, 503, 500.
pub async fn create_basket_from_screen(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    request: Request,
) -> Result<Response, Problem> {
    let received = write_kit::read(request).await?;
    let principal = require_authenticated(&principal)?.clone();
    let entitlements = entitlements_for(&state.db, &principal, &state.settings).await?;
    let mut e = Errors::new();
    let body = body_model::body(&mut e, &received, &from_screen_in(), true);
    e.finish()?;
    let body = body.ok_or_else(internal_error)?;
    scoped_sole_user_id(&state, principal.user_id, None).await?;
    let public_id = body
        .get("screen_public_id")
        .str()
        .unwrap_or_default()
        .to_owned();
    let args = SizeArgs {
        amount: body.get("amount").dec().cloned().unwrap_or_default(),
        profile: body.get("profile").str().and_then(HoldingProfile::parse),
        holdings: body
            .get("holdings")
            .int()
            .and_then(|i| i64::try_from(i).ok()),
        exposure_tier: body.get("exposure_tier").str().map(str::to_owned),
        cash_pct: body.get("cash_pct").dec().cloned(),
        method: body.get("method").str().and_then(WeightMethod::parse),
        custom: None,
    };
    let custom_items: Option<Vec<V>> = match body.get("custom_weights") {
        V::List(items) => Some(items.clone()),
        _ => None,
    };
    let name_in = body.get("name").str().map(str::to_owned);
    let description = body.get("description_md").str().map(str::to_owned);
    let pool = write_kit::pool(write)?;
    let out = session::run(&pool, move |tx| {
        Box::pin(async move {
            let screen = screen_where(tx, "public_id = $1", ScreenKey::Public(&public_id)).await?;
            let Some(screen) = screen.filter(|s| visible_to(s.user_id, &principal)) else {
                return Err(not_found("screen", &public_id));
            };
            let definition = stored_definition(&screen.definition)?;
            super::screens::require_universe(&entitlements, &definition.index)?;
            let mut args = args;
            args.custom = custom_map(custom_items.as_deref())?;
            let (sized, as_of) = sized(tx, &definition, &args, None).await?;
            let symbols: Vec<String> = sized.holdings.iter().map(|h| h.symbol.clone()).collect();
            let by_symbol = instruments_for(tx, &symbols).await?;
            let manager_id = curated_manager_id(tx).await?;
            let today = write_kit::now_utc().date_naive();
            let name = name_in
                .filter(|n| !n.is_empty())
                .unwrap_or_else(|| screen.name.clone())
                .trim_matches(crate::routes::common::py_space)
                .to_owned();
            let slug = slug_for(tx, &name).await?;
            let basket = sqlx::query(
                "INSERT INTO cb_basket (slug, name, manager_id, type, access, visibility, \
                 categories, description_md, rationale_md, rebalance_frequency, \
                 benchmark_instrument_id, launched_at, next_review_at, source, \
                 scan_strategy_key, source_screen_id, archived_at) VALUES ($1, $2, $3, 'STOCK', \
                 'FREE', 'PRIVATE', ARRAY['screen']::varchar[], $4, NULL, 'NEED_BASIS', NULL, $5, \
                 NULL, 'SCREEN', NULL, $6, NULL) RETURNING id, slug, name, visibility, type, source",
            )
            .bind(&slug)
            .bind(&name)
            .bind(manager_id)
            .bind(&description)
            .bind(today)
            .bind(screen.id)
            .fetch_one(&mut **tx)
            .await
            .map_err(db)?;
            let basket_id: i64 = basket.try_get("id").map_err(db)?;
            let notes = format!(
                "Cut from screen {} as of {}, {} weights.",
                baskfy_providers::py::repr_str(&screen.public_id),
                wire::py_date(as_of),
                sized.method.value()
            );
            let version = sqlx::query(
                "INSERT INTO cb_basket_version (basket_id, version_no, effective_date, label, \
                 added_count, removed_count, notes_md, source_scan_run_id) VALUES ($1, 1, $2, \
                 'GENESIS', $3, 0, $4, NULL) RETURNING id, version_no, label",
            )
            .bind(basket_id)
            .bind(as_of)
            .bind(i32::try_from(symbols.len()).map_err(|_| internal_error())?)
            .bind(&notes)
            .fetch_one(&mut **tx)
            .await
            .map_err(db)?;
            let version_id: i64 = version.try_get("id").map_err(db)?;
            let mut rows = Vec::with_capacity(sized.holdings.len());
            let mut holdings = Vec::with_capacity(sized.holdings.len());
            for h in &sized.holdings {
                let id = by_symbol.get(&h.symbol).copied().ok_or_else(internal_error)?;
                rows.push((id, numeric_text(&h.weight)));
                holdings.push(obj(vec![
                    ("rank", OrderedJson::Int(h.rank)),
                    ("symbol", OrderedJson::Str(h.symbol.clone())),
                    ("instrument_id", OrderedJson::Int(id)),
                    ("weight", dec(&h.weight)),
                    ("weight_pct_of_amount", dec(&h.weight_pct_of_amount)),
                    ("amount", dec(&h.amount)),
                ]));
            }
            insert_constituents(tx, version_id, &rows).await?;
            Ok(obj(vec![
                ("id", OrderedJson::Int(basket_id)),
                ("slug", OrderedJson::Str(basket.try_get("slug").map_err(db)?)),
                ("name", OrderedJson::Str(basket.try_get("name").map_err(db)?)),
                (
                    "visibility",
                    OrderedJson::Str(basket.try_get("visibility").map_err(db)?),
                ),
                ("type", OrderedJson::Str(basket.try_get("type").map_err(db)?)),
                ("source", OrderedJson::Str(basket.try_get("source").map_err(db)?)),
                (
                    "version_no",
                    OrderedJson::Int(i64::from(version.try_get::<i32, _>("version_no").map_err(db)?)),
                ),
                ("label", OrderedJson::Str(version.try_get("label").map_err(db)?)),
                ("screen_public_id", OrderedJson::Str(screen.public_id.clone())),
                ("as_of", OrderedJson::Str(wire::py_date(as_of))),
                ("amount", dec(&sized.amount)),
                ("deployed", dec(&sized.deployed)),
                ("cash", dec(&sized.cash)),
                ("cash_pct", dec(&sized.cash_pct)),
                (
                    "profile",
                    sized
                        .profile
                        .map_or(OrderedJson::Null, |p| OrderedJson::Str(p.value().to_owned())),
                ),
                ("holdings_overridden", OrderedJson::Bool(sized.holdings_overridden)),
                (
                    "minimum_amount",
                    sized.minimum.as_ref().map_or(OrderedJson::Null, dec),
                ),
                ("fundable", OrderedJson::Bool(sized.is_fundable())),
                ("method", OrderedJson::Str(sized.method.value().to_owned())),
                ("holdings", OrderedJson::Array(holdings)),
            ]))
        })
    })
    .await?;
    Ok(wire::json_status(StatusCode::CREATED, &out))
}

/// `_method_from_notes(notes)`.
fn method_from_notes(notes: Option<&str>) -> Option<WeightMethod> {
    let re = METHOD_IN_NOTES.as_ref()?;
    let caps = re.captures(notes.unwrap_or(""))?;
    WeightMethod::parse(caps.get(1)?.as_str())
}

/// `POST /cb/baskets/{slug}/recut` (`recut_basket_from_screen`): preview, or the next version.
///
/// # Errors
/// 400, 401, 402, 404, 422, 503, 500.
pub async fn recut_basket_from_screen(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    Path(slug): Path<String>,
    request: Request,
) -> Result<Response, Problem> {
    let received = write_kit::read(request).await?;
    let principal = require_authenticated(&principal)?.clone();
    let entitlements = entitlements_for(&state.db, &principal, &state.settings).await?;
    let mut e = Errors::new();
    let body = body_model::body(&mut e, &received, &recut_in(), true);
    e.finish()?;
    let body = body.ok_or_else(internal_error)?;
    scoped_sole_user_id(&state, principal.user_id, None).await?;
    let confirm = body.get("confirm").bool().unwrap_or(false);
    let holdings_in = body
        .get("holdings")
        .int()
        .and_then(|i| i64::try_from(i).ok());
    let method_in = body.get("method").str().and_then(WeightMethod::parse);
    let pool = write_kit::pool(write)?;
    let out = session::run(&pool, move |tx| {
        Box::pin(async move {
            let basket = sqlx::query(
                "SELECT id, slug, source, source_screen_id FROM cb_basket WHERE slug = $1 AND \
                 archived_at IS NULL",
            )
            .bind(&slug)
            .fetch_optional(&mut **tx)
            .await
            .map_err(db)?
            .ok_or_else(|| not_found("basket", &slug))?;
            let basket_id: i64 = basket.try_get("id").map_err(db)?;
            let basket_slug: String = basket.try_get("slug").map_err(db)?;
            let source: String = basket.try_get("source").map_err(db)?;
            let screen_id: Option<i64> = basket.try_get("source_screen_id").map_err(db)?;
            let Some(screen_id) = screen_id.filter(|_| source == "SCREEN") else {
                return Err(invalid(
                    "only a basket saved from a screen can be re-cut, and this one's screen is \
                     not recorded (or was deleted)",
                ));
            };
            let screen = screen_where(tx, "id = $1", ScreenKey::Id(screen_id)).await?;
            let Some(screen) = screen.filter(|s| visible_to(s.user_id, &principal)) else {
                return Err(not_found("screen", &screen_id.to_string()));
            };
            let definition = stored_definition(&screen.definition)?;
            super::screens::require_universe(&entitlements, &definition.index)?;
            let current = sqlx::query(
                "SELECT id, version_no, notes_md FROM cb_basket_version WHERE basket_id = $1 \
                 ORDER BY version_no DESC LIMIT 1",
            )
            .bind(basket_id)
            .fetch_optional(&mut **tx)
            .await
            .map_err(db)?;
            let mut previous: Vec<(String, WideDecimal)> = Vec::new();
            let mut current_no: i64 = 0;
            let mut current_notes: Option<String> = None;
            if let Some(c) = &current {
                let version_id: i64 = c.try_get("id").map_err(db)?;
                current_no = i64::from(c.try_get::<i32, _>("version_no").map_err(db)?);
                current_notes = c.try_get("notes_md").map_err(db)?;
                let rows = sqlx::query(
                    "SELECT instrument.symbol, cb_constituent.weight::text AS weight FROM \
                     cb_constituent JOIN instrument ON instrument.id = cb_constituent.instrument_id \
                     WHERE cb_constituent.version_id = $1",
                )
                .bind(version_id)
                .fetch_all(&mut **tx)
                .await
                .map_err(db)?;
                for r in &rows {
                    let s: String = r.try_get("symbol").map_err(db)?;
                    let w: String = r.try_get("weight").map_err(db)?;
                    let w: WideDecimal = w.parse().map_err(|_| internal_error())?;
                    let key = s.to_uppercase();
                    match previous.iter_mut().find(|(k, _)| *k == key) {
                        Some(slot) => slot.1 = w,
                        None => previous.push((key, w)),
                    }
                }
            }
            let carried = method_in.or_else(|| method_from_notes(current_notes.as_deref()));
            let fell_back = method_in.is_none() && carried == Some(WeightMethod::Custom);
            let method = match carried {
                None | Some(WeightMethod::Custom) => WeightMethod::Equal,
                Some(m) => m,
            };
            let args = SizeArgs {
                amount: WideDecimal::from(1_000_000_i64),
                profile: None,
                holdings: holdings_in,
                exposure_tier: None,
                cash_pct: None,
                method: Some(method),
                custom: None,
            };
            let default = (!previous.is_empty())
                .then(|| i64::try_from(previous.len()).unwrap_or(i64::MAX).min(MAX_HOLDINGS));
            let (sized, as_of) = sized(tx, &definition, &args, default).await?;
            let new_weights: Vec<(String, WideDecimal)> = sized
                .holdings
                .iter()
                .map(|h| (h.symbol.to_uppercase(), h.weight.clone()))
                .collect();
            let prev_of = |s: &str| previous.iter().find(|(k, _)| k == s).map(|(_, w)| w);
            let new_has = |s: &str| new_weights.iter().any(|(k, _)| k == s);
            let added: Vec<String> = new_weights
                .iter()
                .filter(|(k, _)| prev_of(k).is_none())
                .map(|(k, _)| k.clone())
                .collect();
            let mut removed: Vec<String> = previous
                .iter()
                .filter(|(k, _)| !new_has(k))
                .map(|(k, _)| k.clone())
                .collect();
            removed.sort();
            let kept: Vec<String> = new_weights
                .iter()
                .filter(|(k, _)| prev_of(k).is_some())
                .map(|(k, _)| k.clone())
                .collect();
            let changed = new_weights.len() != previous.len()
                || new_weights
                    .iter()
                    .any(|(k, w)| prev_of(k).is_none_or(|p| p != w));
            let mut version_no: Option<i64> = None;
            if confirm && changed {
                let symbols: Vec<String> = new_weights.iter().map(|(k, _)| k.clone()).collect();
                let by_symbol = instruments_for(tx, &symbols).await?;
                let notes = format!(
                    "Re-cut from screen {} as of {}, {} weights.",
                    baskfy_providers::py::repr_str(&screen.public_id),
                    wire::py_date(as_of),
                    method.value()
                );
                let version = sqlx::query(
                    "INSERT INTO cb_basket_version (basket_id, version_no, effective_date, \
                     label, added_count, removed_count, notes_md, source_scan_run_id) VALUES \
                     ($1, $2, $3, 'CHANGED', $4, $5, $6, NULL) RETURNING id, version_no",
                )
                .bind(basket_id)
                .bind(i32::try_from(current_no + 1).map_err(|_| internal_error())?)
                .bind(as_of)
                .bind(i32::try_from(added.len()).map_err(|_| internal_error())?)
                .bind(i32::try_from(removed.len()).map_err(|_| internal_error())?)
                .bind(&notes)
                .fetch_one(&mut **tx)
                .await
                .map_err(db)?;
                let version_id: i64 = version.try_get("id").map_err(db)?;
                let mut rows = Vec::with_capacity(sized.holdings.len());
                for h in &sized.holdings {
                    let id = by_symbol
                        .get(&h.symbol.to_uppercase())
                        .copied()
                        .ok_or_else(internal_error)?;
                    rows.push((id, numeric_text(&h.weight)));
                }
                insert_constituents(tx, version_id, &rows).await?;
                version_no = Some(i64::from(version.try_get::<i32, _>("version_no").map_err(db)?));
            }
            let strings = |v: &[String]| {
                OrderedJson::Array(v.iter().cloned().map(OrderedJson::Str).collect())
            };
            let holdings: Vec<OrderedJson> = sized
                .holdings
                .iter()
                .map(|h| {
                    obj(vec![
                        ("rank", OrderedJson::Int(h.rank)),
                        ("symbol", OrderedJson::Str(h.symbol.clone())),
                        ("weight", dec(&h.weight)),
                        (
                            "previous_weight",
                            prev_of(&h.symbol.to_uppercase()).map_or(OrderedJson::Null, dec),
                        ),
                    ])
                })
                .collect();
            Ok(obj(vec![
                ("slug", OrderedJson::Str(basket_slug)),
                ("screen_public_id", OrderedJson::Str(screen.public_id.clone())),
                ("as_of", OrderedJson::Str(wire::py_date(as_of))),
                ("method", OrderedJson::Str(method.value().to_owned())),
                ("method_fell_back", OrderedJson::Bool(fell_back)),
                ("current_version_no", OrderedJson::Int(current_no)),
                ("added", strings(&added)),
                ("removed", strings(&removed)),
                ("kept", strings(&kept)),
                ("changed", OrderedJson::Bool(changed)),
                ("written", OrderedJson::Bool(version_no.is_some())),
                (
                    "version_no",
                    version_no.map_or(OrderedJson::Null, OrderedJson::Int),
                ),
                ("holdings", OrderedJson::Array(holdings)),
            ]))
        })
    })
    .await?;
    Ok(wire::json(&out))
}

/// The routes of `baskfy_api.routers.curated_from_screen`, relative to `/api/v1`.
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/cb/baskets/from-screen", post(create_basket_from_screen))
        .route("/cb/baskets/{slug}/recut", post(recut_basket_from_screen))
}
