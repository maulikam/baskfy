//! Port of `baskfy_api.opportunities_llm`: the local model's tool router for the opportunities chat.
//!
//! The model only chooses a tool and its arguments; nothing here can reach an order path.

use std::time::Duration;

use serde_json::{Map, Value, json};

/// Env var naming the OpenAI-compatible base URL; empty disables the router.
pub const URL_ENV: &str = "BASKFY_LLM_URL";
/// Env var naming the model.
pub const MODEL_ENV: &str = "BASKFY_LLM_MODEL";
/// Env var holding the request timeout in seconds.
pub const TIMEOUT_ENV: &str = "BASKFY_LLM_TIMEOUT_S";
/// The model used when none is configured.
pub const DEFAULT_MODEL: &str = "qwen3:8b";
/// The timeout used when none (or an unparseable one) is configured.
pub const DEFAULT_TIMEOUT_S: f64 = 20.0;
/// The swing-scan tool's name.
pub const SWING_TOOL: &str = "scan_swing";
/// The options-comparison tool's name.
pub const OPTIONS_TOOL: &str = "compare_options";

/// `SwingScanIn.model_fields`.
pub const SWING_FIELDS: [&str; 11] = [
    "universe",
    "sectors",
    "setups",
    "min_turnover_cr",
    "min_price",
    "horizon_min",
    "horizon_max",
    "atr_pct_min",
    "atr_pct_max",
    "min_rr",
    "limit",
];
/// `OptionsIn.model_fields` (`quote_rules` is a field, though not offered to the model).
pub const OPTIONS_FIELDS: [&str; 7] = [
    "symbol",
    "direction",
    "strategies",
    "max_loss_inr",
    "min_dte",
    "max_dte",
    "quote_rules",
];

/// The router's system prompt.
pub const SYSTEM_PROMPT: &str = "You route requests for an Indian equity (NSE) trading-opportunity scanner. \
Call exactly one tool when the request asks for swing setups or for option structures \
on one underlying; otherwise call no tool. Use only arguments the user stated or clearly \
implied and leave everything else unset. Symbols are NSE trading symbols in upper case. \
Holding horizons are in trading sessions. Money is in Indian rupees. Never invent prices \
or symbols; if no symbol is named or selected, call no tool.";

/// Where and how to reach the model.
#[derive(Debug, Clone, PartialEq)]
pub struct LlmConfig {
    /// Base URL without a trailing slash.
    pub url: String,
    /// Model name.
    pub model: String,
    /// Request timeout, at least one second.
    pub timeout_s: f64,
}

/// The tool the model chose and its normalised, validated arguments.
#[derive(Debug, Clone, PartialEq)]
pub struct ToolChoice {
    /// [`SWING_TOOL`] or [`OPTIONS_TOOL`].
    pub tool: String,
    /// The arguments, unknown keys and nulls dropped.
    pub arguments: Map<String, Value>,
}

/// Does an argument object validate as the API's request model? The API layer plugs its structs in.
pub trait ToolArgsValidator {
    /// `SwingScanIn.model_validate(arguments)` succeeds.
    fn swing_valid(&self, arguments: &Map<String, Value>) -> bool;
    /// `OptionsIn.model_validate(arguments)` succeeds.
    fn options_valid(&self, arguments: &Map<String, Value>) -> bool;
}

/// The config from the environment, `None` when [`URL_ENV`] is empty.
pub fn config_from_env() -> Option<LlmConfig> {
    let url = std::env::var(URL_ENV).unwrap_or_default();
    let url = url.trim().trim_end_matches('/');
    if url.is_empty() {
        return None;
    }
    let model = std::env::var(MODEL_ENV).unwrap_or_default();
    let model = match model.trim() {
        "" => DEFAULT_MODEL.to_owned(),
        m => m.to_owned(),
    };
    let raw = std::env::var(TIMEOUT_ENV).unwrap_or_default();
    let timeout = if raw.is_empty() {
        DEFAULT_TIMEOUT_S
    } else {
        raw.trim().parse::<f64>().unwrap_or(DEFAULT_TIMEOUT_S)
    };
    Some(LlmConfig {
        url: url.to_owned(),
        model,
        timeout_s: timeout.max(1.0),
    })
}

fn swing_parameters() -> Value {
    let slug = json!({"maxLength": 64, "minLength": 1, "pattern": "^[a-z0-9-]+$", "type": "string"});
    let mut universe = slug.clone();
    if let Value::Object(m) = &mut universe {
        m.insert("default".into(), json!("nifty-500"));
        m.insert("title".into(), json!("Universe"));
    }
    json!({
        "type": "object",
        "properties": {
            "universe": universe,
            "sectors": {"items": slug, "maxItems": 20, "title": "Sectors", "type": "array"},
            "setups": {
                "items": {"enum": ["BREAKOUT", "PULLBACK", "RELATIVE_STRENGTH"], "type": "string"},
                "maxItems": 3, "minItems": 1, "title": "Setups", "type": "array"
            },
            "min_turnover_cr": {"default": 5.0, "maximum": 10000, "minimum": 0, "title": "Min Turnover Cr", "type": "number"},
            "min_price": {"default": 50.0, "maximum": 1_000_000, "minimum": 0, "title": "Min Price", "type": "number"},
            "horizon_min": {"default": 5, "maximum": 120, "minimum": 1, "title": "Horizon Min", "type": "integer"},
            "horizon_max": {"default": 20, "maximum": 120, "minimum": 1, "title": "Horizon Max", "type": "integer"},
            "atr_pct_min": {"default": 1.0, "maximum": 100, "minimum": 0, "title": "Atr Pct Min", "type": "number"},
            "atr_pct_max": {"default": 8.0, "maximum": 100, "minimum": 0, "title": "Atr Pct Max", "type": "number"},
            "min_rr": {"default": 1.5, "maximum": 20, "minimum": 0, "title": "Min Rr", "type": "number"},
            "limit": {"default": 50, "maximum": 100, "minimum": 1, "title": "Limit", "type": "integer"}
        },
        "required": [],
        "additionalProperties": false
    })
}

fn options_parameters() -> Value {
    json!({
        "type": "object",
        "properties": {
            "symbol": {"maxLength": 32, "minLength": 1, "pattern": "^[A-Za-z0-9&-]+$", "title": "Symbol", "type": "string"},
            "direction": {"enum": ["BULLISH", "BEARISH"], "title": "Direction", "type": "string"},
            "strategies": {
                "items": {"enum": ["LONG_CALL", "LONG_PUT", "BULL_CALL_SPREAD", "BEAR_PUT_SPREAD"], "type": "string"},
                "maxItems": 4, "minItems": 1, "title": "Strategies", "type": "array"
            },
            "max_loss_inr": {
                "anyOf": [
                    {"exclusiveMinimum": 0.0, "maximum": 100_000_000.0, "type": "number"},
                    {"pattern": "^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$", "type": "string"},
                    {"type": "null"}
                ],
                "default": null,
                "title": "Max Loss Inr"
            },
            "min_dte": {"default": 1, "maximum": 365, "minimum": 0, "title": "Min Dte", "type": "integer"},
            "max_dte": {"default": 60, "maximum": 365, "minimum": 0, "title": "Max Dte", "type": "integer"}
        },
        "required": ["symbol", "direction"],
        "additionalProperties": false
    })
}

/// The two tools offered to the model (no order tool, no `quote_rules`).
pub fn tool_definitions() -> Value {
    json!([
        {
            "type": "function",
            "function": {
                "name": SWING_TOOL,
                "description": "Scan NSE stocks for swing setups (BREAKOUT, PULLBACK, RELATIVE_STRENGTH) \
on completed daily bars, with liquidity, horizon, volatility and \
reward-to-risk filters. min_turnover_cr is average daily turnover in \
rupee crore; 'liquid' means about 25.",
                "parameters": swing_parameters(),
            }
        },
        {
            "type": "function",
            "function": {
                "name": OPTIONS_TOOL,
                "description": "Compare listed option structures (LONG_CALL, LONG_PUT, BULL_CALL_SPREAD, \
BEAR_PUT_SPREAD) on one NSE underlying for a BULLISH or BEARISH view, \
optionally within a maximum loss in rupees and a days-to-expiry window.",
                "parameters": options_parameters(),
            }
        }
    ])
}

fn state_symbol(state: Option<&str>) -> Option<String> {
    state
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_uppercase)
}

fn grounded_symbol(arguments: &Map<String, Value>, text: &str, selected: Option<&str>) -> Option<String> {
    if let Some(Value::String(proposed)) = arguments.get("symbol") {
        let candidate = proposed.trim().to_uppercase();
        if !candidate.is_empty() {
            let in_text = regex::Regex::new(&format!(r"\b{}\b", regex::escape(&candidate)))
                .is_ok_and(|re| re.is_match(text));
            if selected == Some(candidate.as_str()) || in_text {
                return Some(candidate);
            }
        }
    }
    selected.map(str::to_owned)
}

// Small models add stray keys and write slugs as names ("NIFTY 200"); keep the call.
fn normalised(known: &[&str], arguments: Map<String, Value>) -> Map<String, Value> {
    let mut out: Map<String, Value> = arguments
        .into_iter()
        .filter(|(k, _)| known.contains(&k.as_str()))
        .collect();
    if let Some(Value::String(universe)) = out.get("universe") {
        let lowered = universe.trim().to_lowercase();
        let mut slug = String::with_capacity(lowered.len());
        let mut in_run = false;
        for c in lowered.chars() {
            if c.is_ascii_lowercase() || c.is_ascii_digit() {
                slug.push(c);
                in_run = false;
            } else if !in_run {
                slug.push('-');
                in_run = true;
            }
        }
        let slug = slug.trim_matches('-').to_owned();
        out.insert("universe".into(), Value::String(slug));
    }
    out
}

fn validated(
    name: &str,
    arguments: Map<String, Value>,
    text: &str,
    state_symbol: Option<&str>,
    validator: &dyn ToolArgsValidator,
) -> Option<ToolChoice> {
    if name == SWING_TOOL {
        let arguments = normalised(&SWING_FIELDS, arguments);
        return validator.swing_valid(&arguments).then(|| ToolChoice {
            tool: name.to_owned(),
            arguments,
        });
    }
    if name == OPTIONS_TOOL {
        let mut arguments = normalised(&OPTIONS_FIELDS, arguments);
        let selected = self::state_symbol(state_symbol);
        let symbol = grounded_symbol(&arguments, text, selected.as_deref())?;
        arguments.insert("symbol".into(), Value::String(symbol));
        return validator.options_valid(&arguments).then(|| ToolChoice {
            tool: name.to_owned(),
            arguments,
        });
    }
    None
}

fn first<'a>(value: Option<&'a Value>, key: &str) -> Option<&'a Value> {
    match value?.as_object()?.get(key)? {
        Value::Array(items) => items.first(),
        item => Some(item),
    }
}

/// The model's first tool call, normalised and validated; `None` for no call or a bad one.
pub fn parse_tool_call(
    body: &Value,
    text: &str,
    state_symbol: Option<&str>,
    validator: &dyn ToolArgsValidator,
) -> Option<ToolChoice> {
    let message = first(first(Some(body), "choices"), "message");
    let function = first(first(message, "tool_calls"), "function")?.as_object()?;
    let name = function.get("name")?.as_str()?;
    let raw = match function.get("arguments")? {
        Value::String(s) if s.is_empty() => Value::Object(Map::new()),
        Value::String(s) => serde_json::from_str::<Value>(s).ok()?,
        other => other.clone(),
    };
    let Value::Object(raw) = raw else {
        return None;
    };
    let arguments: Map<String, Value> = raw.into_iter().filter(|(_, v)| !v.is_null()).collect();
    validated(name, arguments, text, state_symbol, validator)
}

/// Asks the model once; any transport, status or parse failure is `None`.
pub async fn choose_tool(
    text: &str,
    state_symbol: Option<&str>,
    config: &LlmConfig,
    validator: &dyn ToolArgsValidator,
) -> Option<ToolChoice> {
    let mut system = SYSTEM_PROMPT.to_owned();
    if let Some(selected) = self::state_symbol(state_symbol) {
        system.push_str(&format!(
            " The candidate currently selected on screen is {selected}."
        ));
    }
    let payload = json!({
        "model": config.model,
        "temperature": 0,
        "reasoning_effort": "none",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ],
        "tools": tool_definitions(),
        "tool_choice": "auto",
    });
    let mut builder = baskfy_providers::net::client_builder();
    if let Ok(timeout) = Duration::try_from_secs_f64(config.timeout_s) {
        builder = builder.timeout(timeout);
    }
    let client = baskfy_providers::net::GuardedClient::from_builder(builder).ok()?;
    let body = serde_json::to_vec(&payload).ok()?;
    let response = client
        .post(&format!("{}/chat/completions", config.url))
        .ok()?
        .header("Content-Type", "application/json")
        .body(body)
        .send()
        .await
        .ok()?;
    if response.status().as_u16() >= 400 {
        return None;
    }
    let bytes = response.bytes().await.ok()?;
    let parsed: Value = serde_json::from_slice(&bytes).ok()?;
    parse_tool_call(&parsed, text, state_symbol, validator)
}
