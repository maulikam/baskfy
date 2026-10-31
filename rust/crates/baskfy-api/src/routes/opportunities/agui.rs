//! Port of `baskfy_api.opportunities_agui`: the AG-UI run (keyword intent parser, LLM router with
//! grounding, event stream). Read-only: the two tools are the swing scan and the options compare.

use std::collections::{BTreeMap, HashMap};
use std::sync::{LazyLock, Mutex};

use baskfy_db::{Db, OrderedJson};
use regex::Regex;
use rust_decimal::Decimal;
use serde_json::Value;
use tokio::sync::mpsc;

use crate::opportunities_llm::{self as llm, OPTIONS_TOOL, SWING_TOOL};
use crate::problems::Problem;
use crate::routes::sleeves::common::{J, Obj, field};
use crate::routes::sleeves::pyd::Fail;
use crate::wire;

use super::schemas::{ArgsValidator, OptionsIn, SwingScanIn};
use super::service::{self, ScanError};
use super::views::{options_json, scan_json};

const MAX_THREADS: usize = 10_000;
const MAX_MESSAGE_CHARS: usize = 2_000;
const MAX_MESSAGES: usize = 100;
const TICKER_STOPWORDS: [&str; 12] = [
    "NSE", "BSE", "INR", "IST", "DTE", "OI", "IV", "ATR", "RR", "CE", "PE", "NIFTY",
];
const UNIVERSES: [(&str, &str); 10] = [
    ("smallcap", "nifty-smallcap-250"),
    ("small cap", "nifty-smallcap-250"),
    ("midcap", "nifty-midcap-150"),
    ("mid cap", "nifty-midcap-150"),
    ("nifty 50", "nifty-50"),
    ("nifty 100", "nifty-100"),
    ("nifty 200", "nifty-200"),
    ("nifty 500", "nifty-500"),
    ("midcap 150", "nifty-midcap-150"),
    ("smallcap 250", "nifty-smallcap-250"),
];
const LIQUID_CR: f64 = 25.0;
const HIGHLY_LIQUID_CR: f64 = 100.0;
const ROLES: [&str; 7] = [
    "user", "assistant", "system", "developer", "tool", "activity", "reasoning",
];

type Rx = LazyLock<Option<Regex>>;

fn re(pattern: &str) -> Option<Regex> {
    Regex::new(pattern).ok()
}

fn is_match(rx: &Rx, text: &str) -> bool {
    rx.as_ref().is_some_and(|r| r.is_match(text))
}

static RANGE_DTE: Rx = LazyLock::new(|| {
    re(r"(\d+)\s*(?:-|\x{2013}|\x{2014}|to)\s*(\d+)\s*(?:trading\s+)?(?:days|dte)")
});
static RANGE_SESSIONS: Rx = LazyLock::new(|| {
    re(r"(\d+)\s*(?:-|\x{2013}|\x{2014}|to)\s*(\d+)\s*(?:trading\s+)?(?:sessions|days)")
});
static SYMBOL_FOR: Rx =
    LazyLock::new(|| re(r"\bfor\s+(?:the\s+)?([A-Z][A-Z0-9&-]{1,19})\b"));
static TICKER: Rx = LazyLock::new(|| re(r"\b[A-Z][A-Z0-9&-]{1,19}\b"));
static MAX_LOSS: Rx = LazyLock::new(|| {
    re(r"(?:max(?:imum)?\s+loss|risk(?:ing)?|lose)\D{0,25}?(?:₹|rs\.?|inr)?\s*([\d,]+(?:\.\d+)?)\s*(k|thousand|lakh|lac)?")
});
static LONG_CALL: Rx = LazyLock::new(|| re(r"\blong calls?\b"));
static LONG_PUT: Rx = LazyLock::new(|| re(r"\blong puts?\b"));
static BEARISH: Rx =
    LazyLock::new(|| re(r"\b(bearish|downside|fall|decline|drop)\b"));
static RR: Rx = LazyLock::new(|| {
    re(r"(?:r:r|r/r|risk.{0,4}reward|reward.{0,4}risk)\D{0,12}(\d+(?:\.\d+)?)")
});
static OPTIONS_WORDS: Rx =
    LazyLock::new(|| re(r"\b(options?|calls?|puts?|spreads?|strikes?|expiry)\b"));
static SWING_WORDS: Rx = LazyLock::new(|| {
    re(r"\b(swing|setups?|breakouts?|pullbacks?|relative strength|scan|liquid|patterns?|vcps?|volatility contraction|twt|weeks tight|episodic pivots?|gap[- ]?ups?|gappers?|flags?|htf|inside bars?|nr7|nr-7|squeeze|52[- ]?(?:week|wk|w)|cups?|bounces?|ema|vbt)\b")
});

/// Setup phrases, most specific first; a matched phrase is consumed so "52 week high
/// breakouts" is not also a plain BREAKOUT.
static SETUP_WORDS: LazyLock<Vec<(Option<Regex>, &'static str)>> = LazyLock::new(|| {
    [
        (r"\b(?:vcps?|volatility contraction(?: patterns?)?)\b", "VCP"),
        (r"\b(?:three|3)[- ]weeks?[- ]tight\b|\btwt\b", "THREE_WEEKS_TIGHT"),
        (r"\bepisodic pivots?\b|\bgap[- ]?ups?(?: on volume)?\b|\bgappers?\b|\bep\b", "EPISODIC_PIVOT"),
        (r"\b(?:high[- ]tight[- ]flags?|htf|flags?)\b", "FLAG"),
        (r"\b(?:inside[- ]bars?|nr-?7|narrow[- ]range|squeeze)\b", "INSIDE_BAR_NR7"),
        (r"\b52[- ]?(?:week|wk|w)[- ]?highs?(?: breakouts?)?\b", "HIGH_52W_BREAKOUT"),
        (r"\bcups?(?: (?:and|with|&) handles?)?\b", "CUP_WITH_HANDLE"),
        (r"\b(?:(?:10|20|50)[- ]?(?:day )?ema|ema|ma|moving[- ]average)[- ]?bounces?\b|\bbounces? off (?:the )?(?:10|20|50)\b|\b(?:10|20|50)[- ]?ema\b", "MA_BOUNCE"),
        (r"\bvbt\b|\bvolume[- ]breakouts?\b", "VBT"),
        (r"\bbreakouts?\b", "BREAKOUT"),
        (r"\bpullbacks?\b", "PULLBACK"),
        (r"\brelative strength\b", "RELATIVE_STRENGTH"),
    ]
    .into_iter()
    .map(|(p, name)| (re(p), name))
    .collect()
});

// ---------------------------------------------------------------------------------------------
// The run input and the thread owners
// ---------------------------------------------------------------------------------------------

/// `RunAgentInput` (the fields the run reads).
#[derive(Debug, Clone)]
pub(crate) struct RunInput {
    pub(crate) thread_id: String,
    pub(crate) run_id: String,
    messages: Vec<(String, Value)>,
    state: Value,
}

fn bounded_str(v: Option<&Value>, loc: &str, errors: &mut Vec<String>) -> String {
    match v {
        None => errors.push(format!("{loc}\n  Field required [type=missing]")),
        Some(Value::String(s)) => {
            let n = s.chars().count();
            if n < 1 {
                errors.push(format!(
                    "{loc}\n  String should have at least 1 character [type=string_too_short]"
                ));
            } else if n > 128 {
                errors.push(format!(
                    "{loc}\n  String should have at most 128 characters [type=string_too_long]"
                ));
            } else {
                return s.clone();
            }
        }
        Some(_) => errors.push(format!("{loc}\n  Input should be a valid string [type=string_type]")),
    }
    String::new()
}

/// `RunAgentInput.model_validate(json.loads(body))`; `Err` is the problem's detail.
pub(crate) fn parse_run_input(body: &[u8]) -> Result<RunInput, String> {
    let raw: Value = serde_json::from_slice(body).map_err(|e| e.to_string())?;
    let Value::Object(obj) = raw else {
        return Err("1 validation error for RunAgentInput\n  Input should be a valid dictionary or instance of RunAgentInput [type=model_type]".to_owned());
    };
    let mut errors: Vec<String> = Vec::new();
    let pick = |alias: &str, name: &str| obj.get(alias).or_else(|| obj.get(name));
    let thread_id = bounded_str(pick("threadId", "thread_id"), "threadId", &mut errors);
    let run_id = bounded_str(pick("runId", "run_id"), "runId", &mut errors);
    let mut messages = Vec::new();
    match obj.get("messages") {
        None => errors.push("messages\n  Field required [type=missing]".to_owned()),
        Some(Value::Array(items)) if items.len() > MAX_MESSAGES => errors.push(format!(
            "messages\n  List should have at most {MAX_MESSAGES} items after validation, not {} [type=too_long]",
            items.len()
        )),
        Some(Value::Array(items)) => {
            for (i, item) in items.iter().enumerate() {
                let Value::Object(m) = item else {
                    errors.push(format!(
                        "messages.{i}\n  Input should be a valid dictionary or instance of AguiMessage [type=model_type]"
                    ));
                    continue;
                };
                bounded_str(m.get("id"), &format!("messages.{i}.id"), &mut errors);
                let role = match m.get("role") {
                    Some(Value::String(r)) if ROLES.contains(&r.as_str()) => r.clone(),
                    None => {
                        errors.push(format!("messages.{i}.role\n  Field required [type=missing]"));
                        String::new()
                    }
                    Some(_) => {
                        errors.push(format!(
                            "messages.{i}.role\n  Input should be 'user', 'assistant', 'system', 'developer', 'tool', 'activity' or 'reasoning' [type=literal_error]"
                        ));
                        String::new()
                    }
                };
                messages.push((role, m.get("content").cloned().unwrap_or(Value::Null)));
            }
        }
        Some(_) => errors.push("messages\n  Input should be a valid list [type=list_type]".to_owned()),
    }
    for key in ["tools", "context"] {
        if let Some(v) = obj.get(key)
            && !matches!(v, Value::Array(_) | Value::Null)
        {
            errors.push(format!("{key}\n  Input should be a valid list [type=list_type]"));
        }
    }
    if !errors.is_empty() {
        let n = errors.len();
        let s = if n == 1 { "" } else { "s" };
        return Err(format!(
            "{n} validation error{s} for RunAgentInput\n{}",
            errors.join("\n")
        ));
    }
    Ok(RunInput {
        thread_id,
        run_id,
        messages,
        state: obj.get("state").cloned().unwrap_or(Value::Null),
    })
}

#[derive(Default)]
struct Owners {
    owner: HashMap<String, (i64, u64)>,
    order: BTreeMap<u64, String>,
    seq: u64,
}

static OWNERS: LazyLock<Mutex<Owners>> = LazyLock::new(|| Mutex::new(Owners::default()));

/// `claim_thread(thread_id, user_id)`: false when another user owns the thread.
pub(crate) fn claim_thread(thread_id: &str, user_id: i64) -> bool {
    let Ok(mut o) = OWNERS.lock() else {
        return false;
    };
    if let Some((owner, seq)) = o.owner.get(thread_id).copied() {
        if owner != user_id {
            return false;
        }
        o.order.remove(&seq);
    }
    o.seq += 1;
    let seq = o.seq;
    o.owner.insert(thread_id.to_owned(), (user_id, seq));
    o.order.insert(seq, thread_id.to_owned());
    while o.owner.len() > MAX_THREADS {
        let Some((_, oldest)) = o.order.pop_first() else {
            break;
        };
        o.owner.remove(&oldest);
    }
    true
}

// ---------------------------------------------------------------------------------------------
// Intent
// ---------------------------------------------------------------------------------------------

type Args = Vec<(String, Value)>;

/// `Intent`.
#[derive(Debug, Clone)]
struct Intent {
    tool: Option<&'static str>,
    arguments: Args,
    note: String,
}

fn set(args: &mut Args, key: &str, value: Value) {
    match args.iter_mut().find(|(k, _)| k == key) {
        Some((_, v)) => *v = value,
        None => args.push((key.to_owned(), value)),
    }
}

fn get<'a>(args: &'a Args, key: &str) -> Option<&'a Value> {
    args.iter().find(|(k, _)| k == key).map(|(_, v)| v)
}

fn last_user_text(messages: &[(String, Value)]) -> String {
    messages
        .iter()
        .rev()
        .find_map(|(role, content)| match content {
            Value::String(s) if role == "user" => Some(s.chars().take(MAX_MESSAGE_CHARS).collect()),
            _ => None,
        })
        .unwrap_or_default()
}

fn int_of(s: &str) -> i64 {
    s.parse::<i64>().unwrap_or(i64::MAX)
}

fn range(text: &str, pattern: &Rx) -> Option<(i64, i64)> {
    let c = pattern.as_ref()?.captures(text)?;
    let (low, high) = (int_of(&c[1]), int_of(&c[2]));
    Some((low.min(high), low.max(high)))
}

fn symbol(original: &str, state: &Value) -> Option<String> {
    if let Some(c) = SYMBOL_FOR.as_ref().and_then(|r| r.captures(original))
        && !TICKER_STOPWORDS.contains(&&c[1])
    {
        return Some(c[1].to_owned());
    }
    if let Some(m) = TICKER
        .as_ref()
        .and_then(|r| r.find_iter(original).find(|m| !TICKER_STOPWORDS.contains(&m.as_str())))
    {
        return Some(m.as_str().to_owned());
    }
    match state.get("symbol") {
        Some(Value::String(s)) if !s.is_empty() => Some(s.to_uppercase()),
        _ => None,
    }
}

fn max_loss(text: &str) -> Option<Decimal> {
    let c = MAX_LOSS.as_ref()?.captures(text)?;
    let mut amount: Decimal = c[1].replace(',', "").parse().ok()?;
    match c.get(2).map(|m| m.as_str()) {
        Some("k" | "thousand") => amount = amount.checked_mul(Decimal::from(1000))?,
        Some("lakh" | "lac") => amount = amount.checked_mul(Decimal::from(100_000))?,
        _ => {}
    }
    (amount > Decimal::ZERO).then_some(amount)
}

fn strings(items: &[&str]) -> Value {
    Value::Array(items.iter().map(|s| Value::String((*s).to_owned())).collect())
}

fn options_intent(text: &str, original: &str, state: &Value) -> Intent {
    let mut strategies: Vec<&str> = Vec::new();
    if text.contains("bull call") {
        strategies.push("BULL_CALL_SPREAD");
    }
    if text.contains("bear put") {
        strategies.push("BEAR_PUT_SPREAD");
    }
    if is_match(&LONG_CALL, text) {
        strategies.push("LONG_CALL");
    }
    if is_match(&LONG_PUT, text) {
        strategies.push("LONG_PUT");
    }
    let bearish = is_match(&BEARISH, text)
        || strategies
            .iter()
            .any(|s| *s == "BEAR_PUT_SPREAD" || *s == "LONG_PUT");
    let direction = if bearish && !text.contains("bullish") {
        "BEARISH"
    } else {
        "BULLISH"
    };
    if strategies.is_empty() {
        strategies = if direction == "BEARISH" {
            vec!["LONG_PUT", "BEAR_PUT_SPREAD"]
        } else {
            vec!["LONG_CALL", "BULL_CALL_SPREAD"]
        };
    }
    let Some(symbol) = symbol(original, state) else {
        return Intent {
            tool: None,
            arguments: Vec::new(),
            note: "Name the underlying, for example: bull call spreads for INFY.".to_owned(),
        };
    };
    let mut args: Args = vec![
        ("symbol".to_owned(), Value::String(symbol.clone())),
        ("direction".to_owned(), Value::String(direction.to_owned())),
        ("strategies".to_owned(), strings(&strategies)),
    ];
    if let Some(loss) = max_loss(text) {
        set(&mut args, "max_loss_inr", Value::String(wire::py_decimal(loss)));
    }
    if let Some((lo, hi)) = range(text, &RANGE_DTE) {
        set(&mut args, "min_dte", Value::from(lo));
        set(&mut args, "max_dte", Value::from(hi));
    }
    Intent {
        tool: Some(OPTIONS_TOOL),
        arguments: args,
        note: format!("Comparing {} on {symbol}.", strategies.join(", ")),
    }
}

fn swing_intent(text: &str) -> Intent {
    let mut args: Args = Vec::new();
    if let Some((lo, hi)) = range(text, &RANGE_SESSIONS) {
        set(&mut args, "horizon_min", Value::from(lo));
        set(&mut args, "horizon_max", Value::from(hi));
    }
    if text.contains("highly liquid") || text.contains("very liquid") {
        set(&mut args, "min_turnover_cr", Value::from(HIGHLY_LIQUID_CR));
    } else if text.contains("liquid") {
        set(&mut args, "min_turnover_cr", Value::from(LIQUID_CR));
    }
    let mut rest = text.to_owned();
    let mut setups: Vec<&str> = Vec::new();
    for (rx, name) in SETUP_WORDS.iter() {
        let Some(rx) = rx else { continue };
        if rx.is_match(&rest) {
            if !setups.contains(name) {
                setups.push(*name);
            }
            rest = rx.replace_all(&rest, " ").into_owned();
        }
    }
    if !setups.is_empty() {
        set(&mut args, "setups", strings(&setups));
    }
    for (phrase, slug) in UNIVERSES {
        if text.contains(phrase) {
            set(&mut args, "universe", Value::String(slug.to_owned()));
        }
    }
    if let Some(c) = RR.as_ref().and_then(|r| r.captures(text))
        && let Ok(v) = c[1].parse::<f64>()
    {
        set(&mut args, "min_rr", Value::from(v));
    }
    Intent {
        tool: Some(SWING_TOOL),
        arguments: args,
        note: "Scanning swing setups with those filters.".to_owned(),
    }
}

fn parse_intent(original: &str, state: &Value) -> Intent {
    let text = original.to_lowercase();
    if is_match(&OPTIONS_WORDS, &text) {
        return options_intent(&text, original, state);
    }
    if is_match(&SWING_WORDS, &text) {
        return swing_intent(&text);
    }
    Intent {
        tool: None,
        arguments: Vec::new(),
        note: "I can scan swing setups (breakout, pullback, relative strength, VCP, three weeks \
               tight, episodic pivot, flag, inside bar / NR7, 52-week high, cup with handle, \
               EMA bounce, VBT) or compare long calls, long puts, bull call spreads and bear put \
               spreads for one underlying."
            .to_owned(),
    }
}

/// `resolve_intent(original, state)`: the model first, then the keywords.
async fn resolve_intent(original: &str, state: &Value) -> Intent {
    let config = llm::config_from_env();
    if let Some(config) = &config
        && !original.trim().is_empty()
    {
        let selected = state.get("symbol").and_then(Value::as_str);
        if let Some(choice) = llm::choose_tool(original, selected, config, &ArgsValidator).await {
            let mut arguments: Args = choice.arguments.into_iter().collect();
            let tool = if choice.tool == OPTIONS_TOOL {
                // Direction and strategies come from the user's words, never the model's guess.
                let grounded = options_intent(&original.to_lowercase(), original, state).arguments;
                for key in ["direction", "strategies"] {
                    let value = get(&grounded, key).or_else(|| get(&arguments, key)).cloned();
                    set(&mut arguments, key, value.unwrap_or(Value::Null));
                }
                OPTIONS_TOOL
            } else {
                // "liquid"/"highly liquid" have fixed meanings here (25 / 100 crore).
                if let Some(liquidity) =
                    get(&swing_intent(&original.to_lowercase()).arguments, "min_turnover_cr").cloned()
                {
                    set(&mut arguments, "min_turnover_cr", liquidity);
                }
                SWING_TOOL
            };
            return Intent {
                tool: Some(tool),
                arguments,
                note: format!(
                    "Interpreted by {} as {}; arguments validated.",
                    config.model, choice.tool
                ),
            };
        }
    }
    let intent = parse_intent(original, state);
    let source = if config.is_some() {
        "model gave no usable tool call; matched by keywords"
    } else {
        "matched by keywords"
    };
    Intent {
        tool: intent.tool,
        arguments: if intent.tool.is_some() { intent.arguments } else { Vec::new() },
        note: format!("{} ({source}.)", intent.note),
    }
}

// ---------------------------------------------------------------------------------------------
// Events and summaries
// ---------------------------------------------------------------------------------------------

fn value_j(v: &Value) -> J {
    match v {
        Value::Null => J::Null,
        Value::Bool(b) => J::Bool(*b),
        Value::Number(n) => match (n.as_i64(), n.as_u64(), n.as_f64()) {
            (Some(i), _, _) => J::Int(i),
            (None, Some(u), _) => i64::try_from(u).map_or(J::Float(u as f64), J::Int),
            (_, _, Some(f)) => J::Float(f),
            _ => J::Null,
        },
        Value::String(s) => J::Str(s.clone()),
        Value::Array(items) => J::Arr(items.iter().map(value_j).collect()),
        Value::Object(m) => J::Obj(m.iter().map(|(k, v)| (k.clone(), value_j(v))).collect()),
    }
}

fn args_j(args: &Args) -> J {
    J::Obj(args.iter().map(|(k, v)| (k.clone(), value_j(v))).collect())
}

fn args_ordered(args: &Args) -> OrderedJson {
    OrderedJson::parse(&args_j(args).canonical()).unwrap_or(OrderedJson::Object(Vec::new()))
}

fn event(kind: &str, fields: Vec<(&str, J)>) -> String {
    let mut obj = Obj::new().put("type", J::s(kind));
    for (k, v) in fields {
        obj = obj.put(k, v);
    }
    let ms = crate::entitlements::utc_now().timestamp_millis();
    obj = obj.put("timestamp", J::Int(ms));
    format!("data: {}\n\n", obj.done().canonical())
}

fn text_events(message: &str) -> Vec<String> {
    let id = crate::logging::new_request_id();
    vec![
        event(
            "TEXT_MESSAGE_START",
            vec![("messageId", J::s(&id)), ("role", J::s("assistant"))],
        ),
        event(
            "TEXT_MESSAGE_CONTENT",
            vec![("messageId", J::s(&id)), ("delta", J::s(message))],
        ),
        event("TEXT_MESSAGE_END", vec![("messageId", J::s(&id))]),
    ]
}

/// Python's `str(value)` of a payload value.
fn py_str(v: Option<&J>) -> String {
    match v {
        None | Some(J::Null) => "None".to_owned(),
        Some(J::Bool(b)) => if *b { "True" } else { "False" }.to_owned(),
        Some(J::Int(i)) => i.to_string(),
        Some(J::Float(f)) => baskfy_core::py::float_repr(*f),
        Some(J::Str(s)) => s.clone(),
        Some(J::Dec(d)) => wire::py_decimal(*d),
        Some(J::NegZero(dp)) => {
            if *dp == 0 {
                "-0".to_owned()
            } else {
                format!("-0.{}", "0".repeat(*dp as usize))
            }
        }
        Some(J::Date(d)) => wire::py_date(*d),
        Some(J::Arr(items)) => format!(
            "[{}]",
            items.iter().map(py_repr).collect::<Vec<_>>().join(", ")
        ),
        Some(other) => other.canonical(),
    }
}

fn py_repr(v: &J) -> String {
    match v {
        J::Str(s) => baskfy_providers::py::repr_str(s),
        J::Dec(_) | J::NegZero(_) => format!("Decimal('{}')", py_str(Some(v))),
        J::Date(d) => format!("datetime.date({}, {}, {})", d.format("%Y"), d.format("%-m"), d.format("%-d")),
        other => py_str(Some(other)),
    }
}

/// `summarize_scan(result)`.
fn summarize_scan(result: &J) -> String {
    let (Some(counts @ J::Obj(_)), Some(J::Arr(rows))) =
        (field(result, "counts"), field(result, "opportunities"))
    else {
        return "The scan returned no result.".to_owned();
    };
    let mut head = format!(
        "Data as of {} ({}). {} setups qualified: {} triggered, {} pending.",
        py_str(field(result, "data_as_of")),
        py_str(field(result, "data_status")),
        py_str(field(counts, "qualified")),
        py_str(field(counts, "triggered")),
        py_str(field(counts, "pending")),
    );
    if !matches!(field(result, "data_status"), Some(J::Str(s)) if s == "FRESH") {
        head.push_str(&format!(" Not actionable: {}", py_str(field(result, "stale_reason"))));
    }
    let mut lines = vec![head];
    for row in rows.iter().take(5) {
        if !matches!(row, J::Obj(_)) {
            continue;
        }
        let price = field(row, "entry").and_then(|e| field(e, "price"));
        let total = field(row, "score").and_then(|s| field(s, "total"));
        lines.push(format!(
            "- {}: {} {}, entry {}, invalidation {}, R:R {}, score {}",
            py_str(field(row, "symbol")),
            py_str(field(row, "setup")),
            py_str(field(row, "status")),
            py_str(price),
            py_str(field(row, "invalidation")),
            py_str(field(row, "risk_reward")),
            py_str(total),
        ));
    }
    lines.push("Scores are rule-based rankings, not probabilities.".to_owned());
    lines.join("\n")
}

fn falsy(v: Option<&J>) -> bool {
    match v {
        None | Some(J::Null | J::NegZero(_)) => true,
        Some(J::Dec(d)) => d.is_zero(),
        Some(J::Bool(b)) => !b,
        Some(J::Int(i)) => *i == 0,
        Some(J::Str(s)) => s.is_empty(),
        Some(J::Arr(a)) => a.is_empty(),
        _ => false,
    }
}

/// `summarize_options(result)`.
fn summarize_options(result: &J) -> String {
    if let Some(provider @ J::Obj(_)) = field(result, "provider")
        && falsy(field(provider, "available"))
    {
        return format!(
            "Options data unavailable: {} Needs: {}",
            py_str(field(provider, "reason")),
            py_str(field(provider, "dependency"))
        );
    }
    let candidates = match field(result, "candidates") {
        Some(J::Arr(c)) if !c.is_empty() => c,
        _ => {
            let extra = match field(result, "warnings") {
                Some(J::Arr(w)) => format!(
                    " {}",
                    w.iter().map(|x| py_str(Some(x))).collect::<Vec<_>>().join("; ")
                ),
                _ => String::new(),
            };
            return format!("No candidate passed the quote and risk checks.{extra}");
        }
    };
    let mut lines = vec![format!(
        "{} candidates ({}):",
        candidates.len(),
        py_str(field(result, "data_status"))
    )];
    for row in candidates.iter().take(5) {
        if !matches!(row, J::Obj(_)) {
            continue;
        }
        let profit = field(row, "max_profit_per_lot");
        let profit = if falsy(profit) { "unlimited".to_owned() } else { py_str(profit) };
        lines.push(format!(
            "- {} {}: max loss/lot {} after costs, max profit/lot {}, breakeven {}",
            py_str(field(row, "strategy")),
            py_str(field(row, "expiry")),
            py_str(field(row, "max_loss_after_costs_per_lot")),
            profit,
            py_str(field(row, "breakevens")),
        ));
    }
    lines.push(
        "Candidates to evaluate, not recommendations; Greeks are model-derived.".to_owned(),
    );
    lines.join("\n")
}

/// `exc.errors()` as Python prints the list (type, loc, msg).
fn errors_repr(fail: &Fail) -> String {
    let Fail::Errors(entries) = fail else {
        return "[]".to_owned();
    };
    let text = |e: &crate::problems::ErrorEntry, k: &str| {
        e.iter()
            .find(|(key, _)| key == k)
            .and_then(|(_, v)| match v {
                OrderedJson::Str(s) => Some(s.clone()),
                _ => None,
            })
            .unwrap_or_default()
    };
    let items: Vec<String> = entries
        .iter()
        .map(|e| {
            let loc = text(e, "field");
            let parts: Vec<String> = if loc == "<root>" {
                Vec::new()
            } else {
                loc.split('.')
                    .map(|p| p.parse::<i64>().map_or_else(|_| baskfy_providers::py::repr_str(p), |i| i.to_string()))
                    .collect()
            };
            let loc = match parts.len() {
                1 => format!("({},)", parts[0]),
                _ => format!("({})", parts.join(", ")),
            };
            format!(
                "{{'type': {}, 'loc': {loc}, 'msg': {}}}",
                baskfy_providers::py::repr_str(&text(e, "type")),
                baskfy_providers::py::repr_str(&text(e, "message")),
            )
        })
        .collect();
    format!("[{}]", items.join(", "))
}

/// How a tool run ended short of a result.
enum Stop {
    /// A `RUN_ERROR` event.
    Error(String),
    /// An unhandled failure: the stream just ends (as Python's generator raising).
    Abort,
}

fn run_error(message: &str, code: &str) -> Stop {
    Stop::Error(event(
        "RUN_ERROR",
        vec![("message", J::s(message)), ("code", J::s(code))],
    ))
}

fn problem_stop(p: &Problem) -> Stop {
    run_error(&p.detail, p.kind.value())
}

fn scan_stop(e: ScanError) -> Stop {
    match e {
        ScanError::Unavailable(msg) => run_error(&msg, "universe_unavailable"),
        ScanError::Cancelled | ScanError::Failed(_) => Stop::Abort,
    }
}

/// `_execute(intent, session, user_id)`: the payload and its summary.
async fn execute(intent: &Intent, db: &Db, user_id: i64) -> Result<(J, String), Stop> {
    let now = crate::entitlements::utc_now();
    let args = args_ordered(&intent.arguments);
    if intent.tool == Some(SWING_TOOL) {
        let body = SwingScanIn::from_args(&args).map_err(|f| {
            run_error(&format!("Invalid tool arguments: {}", errors_repr(&f)), "invalid_arguments")
        })?;
        let scan = service::run_swing_scan(db.pool(), &body.universe, &body.filters(), now, None)
            .await
            .map_err(scan_stop)?;
        let payload = scan_json(&scan);
        let summary = summarize_scan(&payload);
        return Ok((payload, summary));
    }
    let options = OptionsIn::from_args(&args)
        .map_err(|f| {
            run_error(&format!("Invalid tool arguments: {}", errors_repr(&f)), "invalid_arguments")
        })?
        .map_err(|_| Stop::Abort)?;
    crate::routes::common::scoped_sole_user_id(db, Some(user_id), Some("opportunities"))
        .await
        .map_err(|p| problem_stop(&p))?;
    let analysis = service::run_options_analysis(&options.request(), now, options.rules.max_quote_age_s)
        .await
        .map_err(|_| Stop::Abort)?;
    let payload = options_json(&options.symbol, &analysis).map_err(|_| Stop::Abort)?;
    let summary = summarize_options(&payload);
    Ok((payload, summary))
}

/// `run(run_input, ...)`: the event stream, sent frame by frame until the client goes away.
pub(crate) async fn run(input: RunInput, db: Db, user_id: i64, tx: mpsc::Sender<String>) {
    let ids = || {
        vec![
            ("threadId", J::s(&input.thread_id)),
            ("runId", J::s(&input.run_id)),
        ]
    };
    macro_rules! emit {
        ($frame:expr) => {
            if tx.send($frame).await.is_err() {
                return;
            }
        };
    }
    emit!(event("RUN_STARTED", ids()));
    emit!(event("STEP_STARTED", vec![("stepName", J::s("interpret"))]));
    let intent = resolve_intent(&last_user_text(&input.messages), &input.state).await;
    emit!(event("STEP_FINISHED", vec![("stepName", J::s("interpret"))]));
    for frame in text_events(&intent.note) {
        emit!(frame);
    }
    let Some(tool) = intent.tool else {
        emit!(event("RUN_FINISHED", ids()));
        return;
    };
    let call_id = crate::logging::new_request_id();
    let parent = crate::logging::new_request_id();
    emit!(event(
        "TOOL_CALL_START",
        vec![
            ("toolCallId", J::s(&call_id)),
            ("toolCallName", J::s(tool)),
            ("parentMessageId", J::s(&parent)),
        ],
    ));
    emit!(event(
        "TOOL_CALL_ARGS",
        vec![
            ("toolCallId", J::s(&call_id)),
            ("delta", J::Str(args_j(&intent.arguments).canonical())),
        ],
    ));
    emit!(event("TOOL_CALL_END", vec![("toolCallId", J::s(&call_id))]));
    emit!(event("STEP_STARTED", vec![("stepName", J::s(tool))]));
    if tx.is_closed() {
        return;
    }
    let (payload, summary) = match execute(&intent, &db, user_id).await {
        Ok(v) => v,
        Err(Stop::Error(frame)) => {
            let _ = tx.send(frame).await;
            return;
        }
        Err(Stop::Abort) => return,
    };
    emit!(event("STEP_FINISHED", vec![("stepName", J::s(tool))]));
    emit!(event(
        "TOOL_CALL_RESULT",
        vec![
            ("messageId", J::Str(crate::logging::new_request_id())),
            ("toolCallId", J::s(&call_id)),
            ("content", J::Str(payload.canonical())),
            ("role", J::s("tool")),
        ],
    ));
    emit!(event(
        "STATE_SNAPSHOT",
        vec![(
            "snapshot",
            Obj::new()
                .put("lastTool", J::s(tool))
                .put("arguments", args_j(&intent.arguments))
                .done(),
        )],
    ));
    for frame in text_events(&summary) {
        emit!(frame);
    }
    emit!(event("RUN_FINISHED", ids()));
}
