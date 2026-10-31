//! Port of `baskfy_api.opportunities_schemas`: the request bodies, as pydantic validates them.

use std::collections::BTreeSet;

use baskfy_core::opportunities::config::{
    Direction, QuoteRules, SetupType, StrategyKind, SwingFilters,
};
use baskfy_core::opportunities::options::OptionsRequest;
use baskfy_db::OrderedJson;
use chrono::NaiveDate;
use rust_decimal::Decimal;
use serde_json::{Map, Value};

use crate::body::Received;
use crate::opportunities_llm::ToolArgsValidator;
use crate::problems::Problem;
use crate::routes::sleeves::pyd::{Bound, Default as D, Fail, Model, PV, Ty, validate_body, validate_value};

use super::service::MAX_VALIDATION_DAYS;

/// `DEFAULT_UNIVERSE`.
pub(crate) const DEFAULT_UNIVERSE: &str = "nifty-500";
const SETUP_NAMES: &[&str] = &["BREAKOUT", "PULLBACK", "RELATIVE_STRENGTH"];
const STRATEGY_NAMES: &[&str] = &["LONG_CALL", "LONG_PUT", "BULL_CALL_SPREAD", "BEAR_PUT_SPREAD"];
const DIRECTIONS: &[&str] = &["BULLISH", "BEARISH"];

fn is_slug(s: &str) -> bool {
    !s.is_empty()
        && s.chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-')
}

fn is_symbol(s: &str) -> bool {
    !s.is_empty()
        && s.chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '&' || c == '-')
}

fn slug() -> Ty {
    Ty::Str {
        min: Some(1),
        max: Some(64),
        pattern: Some(("^[a-z0-9-]+$", is_slug)),
    }
}

fn float(ge: &'static str, le: &'static str) -> Ty {
    Ty::Float(vec![Bound::Ge(ge), Bound::Le(le)])
}

fn int(ge: &'static str, le: &'static str) -> Ty {
    Ty::Int(vec![Bound::Ge(ge), Bound::Le(le)])
}

fn field<'a>(fields: &'a [(String, Option<PV>)], name: &str) -> Option<&'a PV> {
    fields
        .iter()
        .find(|(n, _)| n == name)
        .and_then(|(_, v)| v.as_ref())
}

fn pv_f64(v: Option<&PV>, default: f64) -> f64 {
    match v {
        Some(PV::Dec(d)) => d.py_str().parse::<f64>().unwrap_or(default),
        Some(PV::Int(i)) => i.value.map_or(default, |x| x as f64),
        _ => default,
    }
}

fn pv_i64(v: Option<&PV>, default: i64) -> i64 {
    match v {
        Some(PV::Int(i)) => i
            .value
            .and_then(|x| i64::try_from(x).ok())
            .unwrap_or(default),
        _ => default,
    }
}

fn pv_str(v: Option<&PV>, default: &str) -> String {
    match v {
        Some(PV::Str(s)) => s.clone(),
        _ => default.to_owned(),
    }
}

fn pv_strs(v: Option<&PV>) -> Option<Vec<String>> {
    match v {
        Some(PV::List(items)) => Some(
            items
                .iter()
                .filter_map(|i| match i {
                    PV::Str(s) => Some(s.clone()),
                    _ => None,
                })
                .collect(),
        ),
        _ => None,
    }
}

fn pv_dec(v: Option<&PV>) -> Option<Decimal> {
    match v {
        Some(PV::Dec(d)) => d.to_decimal(),
        Some(PV::Int(i)) => i.value.and_then(|x| Decimal::try_from_i128_with_scale(x, 0).ok()),
        _ => None,
    }
}

fn setup_of(name: &str) -> Option<SetupType> {
    SetupType::ALL.into_iter().find(|s| s.value() == name)
}

fn strategy_of(name: &str) -> Option<StrategyKind> {
    [
        StrategyKind::LongCall,
        StrategyKind::LongPut,
        StrategyKind::BullCallSpread,
        StrategyKind::BearPutSpread,
    ]
    .into_iter()
    .find(|s| s.value() == name)
}

/// `SwingScanIn`.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct SwingScanIn {
    pub(crate) universe: String,
    pub(crate) sectors: Vec<String>,
    pub(crate) setups: Vec<String>,
    pub(crate) min_turnover_cr: f64,
    pub(crate) min_price: f64,
    pub(crate) horizon_min: i64,
    pub(crate) horizon_max: i64,
    pub(crate) atr_pct_min: f64,
    pub(crate) atr_pct_max: f64,
    pub(crate) min_rr: f64,
    pub(crate) limit: i64,
}

impl Default for SwingScanIn {
    fn default() -> Self {
        let f = SwingFilters::default();
        Self {
            universe: DEFAULT_UNIVERSE.to_owned(),
            sectors: Vec::new(),
            setups: SETUP_NAMES.iter().map(|s| (*s).to_owned()).collect(),
            min_turnover_cr: f.min_turnover_cr,
            min_price: f.min_price,
            horizon_min: f.horizon_min,
            horizon_max: f.horizon_max,
            atr_pct_min: f.atr_pct_min,
            atr_pct_max: f.atr_pct_max,
            min_rr: f.min_rr,
            limit: f.limit,
        }
    }
}

fn swing_after(fields: &[(String, Option<PV>)]) -> Result<(), String> {
    let d = SwingScanIn::default();
    if pv_i64(field(fields, "horizon_min"), d.horizon_min)
        > pv_i64(field(fields, "horizon_max"), d.horizon_max)
    {
        return Err("horizon_min must not exceed horizon_max".to_owned());
    }
    if pv_f64(field(fields, "atr_pct_min"), d.atr_pct_min)
        > pv_f64(field(fields, "atr_pct_max"), d.atr_pct_max)
    {
        return Err("atr_pct_min must not exceed atr_pct_max".to_owned());
    }
    Ok(())
}

fn swing_model() -> Model {
    // `le=MAX_RESULTS`.
    let max_results: &'static str = "100";
    Model {
        fields: vec![
            ("universe", slug(), D::Unset),
            (
                "sectors",
                Ty::BoundedList(Box::new(slug()), None, Some(20)),
                D::Unset,
            ),
            (
                "setups",
                Ty::BoundedList(Box::new(Ty::StrLit(SETUP_NAMES)), Some(1), Some(3)),
                D::Unset,
            ),
            ("min_turnover_cr", float("0", "10000"), D::Unset),
            ("min_price", float("0", "1000000"), D::Unset),
            ("horizon_min", int("1", "120"), D::Unset),
            ("horizon_max", int("1", "120"), D::Unset),
            ("atr_pct_min", float("0", "100"), D::Unset),
            ("atr_pct_max", float("0", "100"), D::Unset),
            ("min_rr", float("0", "20"), D::Unset),
            ("limit", int("1", max_results), D::Unset),
        ],
        forbid_extra: true,
        before: None,
        after: Some(swing_after),
    }
}

impl SwingScanIn {
    fn from_pv(pv: &PV) -> Self {
        let d = Self::default();
        let f = |n: &str| pv.field(n);
        Self {
            universe: pv_str(f("universe"), &d.universe),
            sectors: pv_strs(f("sectors")).unwrap_or(d.sectors),
            setups: pv_strs(f("setups")).unwrap_or(d.setups),
            min_turnover_cr: pv_f64(f("min_turnover_cr"), d.min_turnover_cr),
            min_price: pv_f64(f("min_price"), d.min_price),
            horizon_min: pv_i64(f("horizon_min"), d.horizon_min),
            horizon_max: pv_i64(f("horizon_max"), d.horizon_max),
            atr_pct_min: pv_f64(f("atr_pct_min"), d.atr_pct_min),
            atr_pct_max: pv_f64(f("atr_pct_max"), d.atr_pct_max),
            min_rr: pv_f64(f("min_rr"), d.min_rr),
            limit: pv_i64(f("limit"), d.limit),
        }
    }

    /// `SwingScanIn` from a request body.
    pub(crate) fn from_body(received: &Received) -> Result<Self, Problem> {
        validate_body(received, &swing_model())
            .map(|pv| Self::from_pv(&pv))
            .map_err(|f| f.into_problem(Vec::new()))
    }

    /// `SwingScanIn.model_validate(arguments)`.
    pub(crate) fn from_args(args: &OrderedJson) -> Result<Self, Fail> {
        validate_value(args, &Ty::Model(Box::new(swing_model()))).map(|pv| Self::from_pv(&pv))
    }

    /// `filters()`.
    pub(crate) fn filters(&self) -> SwingFilters {
        SwingFilters {
            setups: self.setups.iter().filter_map(|s| setup_of(s)).collect(),
            sectors: self.sectors.iter().cloned().collect::<BTreeSet<String>>(),
            min_turnover_cr: self.min_turnover_cr,
            min_price: self.min_price,
            horizon_min: self.horizon_min,
            horizon_max: self.horizon_max,
            atr_pct_min: self.atr_pct_min,
            atr_pct_max: self.atr_pct_max,
            min_rr: self.min_rr,
            limit: self.limit,
        }
    }
}

/// `ValidateIn`.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct ValidateIn {
    pub(crate) setup: SetupType,
    pub(crate) universe: String,
    pub(crate) start: NaiveDate,
    pub(crate) end: NaiveDate,
    pub(crate) horizon_max: i64,
    pub(crate) slippage_bps: f64,
}

fn validate_after(fields: &[(String, Option<PV>)]) -> Result<(), String> {
    if let (Some(PV::Date(start)), Some(PV::Date(end))) = (field(fields, "start"), field(fields, "end")) {
        if start > end {
            return Err("start must not be after end".to_owned());
        }
        if (*end - *start).num_days() > MAX_VALIDATION_DAYS {
            return Err("the test window is limited to three years".to_owned());
        }
    }
    Ok(())
}

impl ValidateIn {
    /// `ValidateIn` from a request body.
    pub(crate) fn from_body(received: &Received) -> Result<Self, Problem> {
        let model = Model {
            fields: vec![
                ("setup", Ty::StrLit(SETUP_NAMES), D::Required),
                ("universe", slug(), D::Unset),
                ("start", Ty::Date, D::Required),
                ("end", Ty::Date, D::Required),
                ("horizon_max", int("1", "120"), D::Unset),
                ("slippage_bps", float("0", "500"), D::Unset),
            ],
            forbid_extra: true,
            before: None,
            after: Some(validate_after),
        };
        let pv = validate_body(received, &model).map_err(|f| f.into_problem(Vec::new()))?;
        let (Some(PV::Date(start)), Some(PV::Date(end))) = (pv.field("start"), pv.field("end"))
        else {
            return Err(crate::problems::internal_error());
        };
        let setup = match pv.field("setup") {
            Some(PV::Str(s)) => setup_of(s),
            _ => None,
        }
        .ok_or_else(crate::problems::internal_error)?;
        Ok(Self {
            setup,
            universe: pv_str(pv.field("universe"), DEFAULT_UNIVERSE),
            start: *start,
            end: *end,
            horizon_max: pv_i64(pv.field("horizon_max"), 20),
            slippage_bps: pv_f64(pv.field("slippage_bps"), 10.0),
        })
    }
}

/// `OptionsIn` (with its `QuoteRulesIn`).
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct OptionsIn {
    pub(crate) symbol: String,
    pub(crate) direction: Direction,
    pub(crate) strategies: Vec<String>,
    pub(crate) max_loss_inr: Option<Decimal>,
    pub(crate) min_dte: i64,
    pub(crate) max_dte: i64,
    pub(crate) rules: QuoteRules,
}

fn options_after(fields: &[(String, Option<PV>)]) -> Result<(), String> {
    if pv_i64(field(fields, "min_dte"), 1) > pv_i64(field(fields, "max_dte"), 60) {
        return Err("min_dte must not exceed max_dte".to_owned());
    }
    Ok(())
}

fn options_model() -> Model {
    let quote_rules = Model {
        fields: vec![
            (
                "max_spread_pct",
                Ty::Dec(vec![Bound::Gt("0"), Bound::Le("100")]),
                D::Unset,
            ),
            ("max_quote_age_s", int("1", "86400"), D::Unset),
            ("min_oi", Ty::Int(vec![Bound::Ge("0")]), D::Unset),
            ("min_volume", Ty::Int(vec![Bound::Ge("0")]), D::Unset),
        ],
        forbid_extra: true,
        before: None,
        after: None,
    };
    Model {
        fields: vec![
            (
                "symbol",
                Ty::Str {
                    min: Some(1),
                    max: Some(32),
                    pattern: Some(("^[A-Za-z0-9&-]+$", is_symbol)),
                },
                D::Required,
            ),
            ("direction", Ty::StrLit(DIRECTIONS), D::Required),
            (
                "strategies",
                Ty::BoundedList(Box::new(Ty::StrLit(STRATEGY_NAMES)), Some(1), Some(4)),
                D::Unset,
            ),
            (
                "max_loss_inr",
                Ty::Dec(vec![Bound::Gt("0"), Bound::Le("100000000")]).opt(),
                D::Unset,
            ),
            ("min_dte", int("0", "365"), D::Unset),
            ("max_dte", int("0", "365"), D::Unset),
            ("quote_rules", Ty::Model(Box::new(quote_rules)), D::Unset),
        ],
        forbid_extra: true,
        before: None,
        after: Some(options_after),
    }
}

impl OptionsIn {
    fn from_pv(pv: &PV) -> Result<Self, Problem> {
        let d = QuoteRules::default();
        let rules = match pv.field("quote_rules") {
            Some(q) => QuoteRules {
                max_spread_pct: match q.field("max_spread_pct") {
                    None => d.max_spread_pct,
                    some => pv_dec(some).ok_or_else(crate::problems::internal_error)?,
                },
                max_quote_age_s: pv_i64(q.field("max_quote_age_s"), d.max_quote_age_s),
                min_oi: pv_i64(q.field("min_oi"), d.min_oi),
                min_volume: pv_i64(q.field("min_volume"), d.min_volume),
            },
            None => d,
        };
        let max_loss_inr = match pv.field("max_loss_inr") {
            None | Some(PV::Null) => None,
            some => Some(pv_dec(some).ok_or_else(crate::problems::internal_error)?),
        };
        Ok(Self {
            symbol: pv_str(pv.field("symbol"), ""),
            direction: if pv_str(pv.field("direction"), "") == "BEARISH" {
                Direction::Bearish
            } else {
                Direction::Bullish
            },
            strategies: pv_strs(pv.field("strategies"))
                .unwrap_or_else(|| STRATEGY_NAMES.iter().map(|s| (*s).to_owned()).collect()),
            max_loss_inr,
            min_dte: pv_i64(pv.field("min_dte"), 1),
            max_dte: pv_i64(pv.field("max_dte"), 60),
            rules,
        })
    }

    /// `OptionsIn` from a request body.
    pub(crate) fn from_body(received: &Received) -> Result<Self, Problem> {
        let pv = validate_body(received, &options_model()).map_err(|f| f.into_problem(Vec::new()))?;
        Self::from_pv(&pv)
    }

    /// `OptionsIn.model_validate(arguments)`; the outer error is a validation failure.
    pub(crate) fn from_args(args: &OrderedJson) -> Result<Result<Self, Problem>, Fail> {
        validate_value(args, &Ty::Model(Box::new(options_model()))).map(|pv| Self::from_pv(&pv))
    }

    /// `request()`.
    pub(crate) fn request(&self) -> OptionsRequest {
        OptionsRequest {
            underlying: self.symbol.to_uppercase(),
            direction: self.direction,
            strategies: self.strategies.iter().filter_map(|s| strategy_of(s)).collect(),
            max_loss_inr: self.max_loss_inr,
            min_dte: self.min_dte,
            max_dte: self.max_dte,
            rules: self.rules,
        }
    }
}

/// A `serde_json` object as `json.loads` would have produced it.
pub(crate) fn ordered(args: &Map<String, Value>) -> OrderedJson {
    let text = serde_json::to_string(args).unwrap_or_else(|_| "{}".to_owned());
    OrderedJson::parse(&text).unwrap_or(OrderedJson::Object(Vec::new()))
}

/// The LLM router's validator: the same models the routes use.
pub(crate) struct ArgsValidator;

impl ToolArgsValidator for ArgsValidator {
    fn swing_valid(&self, arguments: &Map<String, Value>) -> bool {
        SwingScanIn::from_args(&ordered(arguments)).is_ok()
    }

    fn options_valid(&self, arguments: &Map<String, Value>) -> bool {
        matches!(OptionsIn::from_args(&ordered(arguments)), Ok(Ok(_)))
    }
}
