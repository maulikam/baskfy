//! SW11 — the S2 Kite timing probe (`tasks/swing_timing_probe.py`; STANDING-ANSWERS A4).
//!
//! One morning of Kite timing samples written down: quotes through the pre-open and around the
//! open, three minute-candle asks for the 09:20 bar, the Markdown report, and the `.done` marker
//! after a good run that makes the Beat entry a one-shot. Pure over its source and its clock
//! (the task's clock, which advances in real time, so a wait for a scheduled moment is a real
//! wait). Reads only — `/quote` and `historical_data` through the provider's limiter (law 2).

use std::path::Path;

use baskfy_db::OrderedJson;
use baskfy_providers::kite::KiteProvider;
use baskfy_providers::records::{PyDateTime, QuoteRecord};
use chrono::{NaiveDate, NaiveDateTime, NaiveTime};
use rust_decimal::Decimal;
use sqlx::PgConnection;

use super::TaskContext;
use crate::error::WorkerError;
use crate::pyjson::{int, object, s, strings};

/// What the task writes, and what tells it not to run again.
pub(super) const REPORT_NAME: &str = "S2-kite-timing.md";
pub(super) const DONE_MARKER: &str = "S2-kite-timing.done";

/// The liquid names the probe quotes when the instrument table has them.
const PROBE_SYMBOLS: [&str; 5] = ["RELIANCE", "TCS", "HDFCBANK", "INFY", "SBIN"];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Kind {
    Quote,
    Candle,
}

impl Kind {
    const fn name(self) -> &'static str {
        match self {
            Self::Quote => "quote",
            Self::Candle => "candle",
        }
    }
}

fn t(h: u32, m: u32, s: u32) -> NaiveTime {
    NaiveTime::from_hms_opt(h, m, s).unwrap_or(NaiveTime::MIN)
}

/// `SCHEDULE`.
fn schedule() -> [(NaiveTime, Kind); 13] {
    [
        (t(9, 4, 30), Kind::Quote),
        (t(9, 6, 0), Kind::Quote),
        (t(9, 7, 30), Kind::Quote),
        (t(9, 8, 0), Kind::Quote),
        (t(9, 8, 30), Kind::Quote),
        (t(9, 9, 0), Kind::Quote),
        (t(9, 12, 0), Kind::Quote),
        (t(9, 15, 30), Kind::Quote),
        (t(9, 16, 0), Kind::Quote),
        (t(9, 20, 5), Kind::Candle),
        (t(9, 20, 35), Kind::Candle),
        (t(9, 21, 5), Kind::Candle),
        (t(9, 21, 10), Kind::Quote),
    ]
}

/// `_CANDLE_ASKS`.
const CANDLE_ASKS: usize = 3;

/// `time.isoformat()` (no microseconds in the schedule).
fn time_iso(when: NaiveTime) -> String {
    when.format("%H:%M:%S").to_string()
}

/// `dt.datetime(...).isoformat(timespec="seconds")` for an IST-aware moment.
fn ist_iso(at: NaiveDateTime) -> String {
    format!("{}+05:30", at.format("%Y-%m-%dT%H:%M:%S"))
}

/// `QuoteSample`.
#[derive(Debug, Clone)]
struct QuoteSample {
    at: NaiveDateTime,
    symbol: String,
    last_price: Decimal,
    open: Option<Decimal>,
    volume: i64,
    as_of: Option<PyDateTime>,
}

fn as_of_iso(value: &PyDateTime) -> String {
    match value {
        PyDateTime::Naive(n) => n.format("%Y-%m-%dT%H:%M:%S").to_string(),
        PyDateTime::Aware(a) => a.format("%Y-%m-%dT%H:%M:%S%:z").to_string(),
    }
}

fn as_of_clock(value: &PyDateTime) -> String {
    match value {
        PyDateTime::Naive(n) => n.format("%H:%M:%S").to_string(),
        PyDateTime::Aware(a) => a.format("%H:%M:%S").to_string(),
    }
}

impl QuoteSample {
    fn as_dict(&self) -> OrderedJson {
        object([
            ("at", s(ist_iso(self.at))),
            ("symbol", s(self.symbol.clone())),
            ("last_price", s(self.last_price.to_string())),
            (
                "open",
                crate::pyjson::opt_s(self.open.map(|d| d.to_string())),
            ),
            ("volume", int(self.volume)),
            (
                "as_of",
                crate::pyjson::opt_s(self.as_of.as_ref().map(as_of_iso)),
            ),
        ])
    }
}

/// `CandleSample`.
#[derive(Debug, Clone)]
struct CandleSample {
    at: NaiveDateTime,
    token: i64,
    count: i64,
    last_start: Option<NaiveDateTime>,
    error: Option<String>,
}

impl CandleSample {
    fn as_dict(&self) -> OrderedJson {
        object([
            ("at", s(ist_iso(self.at))),
            ("token", int(self.token)),
            ("count", int(self.count)),
            (
                "last_start",
                crate::pyjson::opt_s(
                    self.last_start
                        .map(|d| d.format("%Y-%m-%dT%H:%M:%S").to_string()),
                ),
            ),
            ("error", crate::pyjson::opt_s(self.error.clone())),
        ])
    }
}

/// `ProbeReport`.
#[derive(Debug, Clone)]
struct ProbeReport {
    day: NaiveDate,
    symbols: Vec<String>,
    token: i64,
    quotes: Vec<QuoteSample>,
    candles: Vec<CandleSample>,
    skipped: Vec<String>,
    errors: Vec<String>,
}

fn early(q: &QuoteSample) -> bool {
    q.at.time() <= t(9, 9, 0)
}

impl ProbeReport {
    fn preopen_volume_seen(&self) -> Option<bool> {
        let early: Vec<&QuoteSample> = self.quotes.iter().filter(|q| early(q)).collect();
        if early.is_empty() {
            return None;
        }
        Some(early.iter().any(|q| q.volume > 0))
    }

    fn preopen_ltp_moved(&self) -> Option<bool> {
        let early: Vec<&QuoteSample> = self.quotes.iter().filter(|q| early(q)).collect();
        let moments: std::collections::BTreeSet<NaiveDateTime> =
            early.iter().map(|q| q.at).collect();
        if moments.len() < 2 {
            return None;
        }
        let mut by_symbol: std::collections::BTreeMap<&str, std::collections::BTreeSet<Decimal>> =
            std::collections::BTreeMap::new();
        for q in early {
            by_symbol
                .entry(q.symbol.as_str())
                .or_default()
                .insert(q.last_price.normalize());
        }
        Some(by_symbol.values().any(|p| p.len() > 1))
    }

    fn open_before_session(&self) -> Option<bool> {
        let early: Vec<&QuoteSample> = self.quotes.iter().filter(|q| early(q)).collect();
        if early.is_empty() {
            return None;
        }
        Some(
            early
                .iter()
                .any(|q| q.open.is_some_and(|o| o > Decimal::ZERO)),
        )
    }

    fn candle_first_seen_at(&self) -> Option<NaiveDateTime> {
        self.candles
            .iter()
            .find(|c| c.last_start.is_some_and(|l| l.time() >= t(9, 20, 0)))
            .map(|c| c.at)
    }

    fn good(&self) -> bool {
        let pre = self.quotes.iter().any(early);
        let post = self.quotes.iter().any(|q| q.at.time() >= t(9, 15, 30));
        let candles_ok =
            self.candles.len() == CANDLE_ASKS && self.candles.iter().all(|c| c.error.is_none());
        pre && post && candles_ok && self.candle_first_seen_at().is_some()
    }

    fn as_detail(&self) -> Vec<(String, OrderedJson)> {
        let opt_bool = |b: Option<bool>| b.map_or(OrderedJson::Null, OrderedJson::Bool);
        vec![
            ("day".to_owned(), s(self.day.format("%Y-%m-%d").to_string())),
            ("symbols".to_owned(), strings(self.symbols.clone())),
            ("token".to_owned(), int(self.token)),
            (
                "quotes".to_owned(),
                OrderedJson::Array(self.quotes.iter().map(QuoteSample::as_dict).collect()),
            ),
            (
                "candles".to_owned(),
                OrderedJson::Array(self.candles.iter().map(CandleSample::as_dict).collect()),
            ),
            ("skipped".to_owned(), strings(self.skipped.clone())),
            ("errors".to_owned(), strings(self.errors.clone())),
            (
                "answers".to_owned(),
                object([
                    ("preopen_volume_seen", opt_bool(self.preopen_volume_seen())),
                    ("preopen_ltp_moved", opt_bool(self.preopen_ltp_moved())),
                    ("open_before_session", opt_bool(self.open_before_session())),
                    (
                        "candle_first_seen_at",
                        crate::pyjson::opt_s(self.candle_first_seen_at().map(ist_iso)),
                    ),
                ]),
            ),
            ("good".to_owned(), OrderedJson::Bool(self.good())),
        ]
    }
}

/// `_probe_names`: `(symbol, kite_token)` for the well-known five, else any five with a token.
pub(super) async fn probe_names(
    conn: &mut PgConnection,
) -> Result<Vec<(String, i64)>, WorkerError> {
    let wanted: Vec<String> = PROBE_SYMBOLS.iter().map(|s| (*s).to_owned()).collect();
    let mut rows: Vec<(String, i64)> = sqlx::query_as(
        "SELECT instrument.symbol, instrument.kite_token FROM instrument \
         WHERE instrument.symbol = ANY($1) AND instrument.kite_token IS NOT NULL \
         ORDER BY instrument.symbol",
    )
    .bind(&wanted)
    .fetch_all(&mut *conn)
    .await?;
    if rows.is_empty() {
        rows = sqlx::query_as(
            "SELECT instrument.symbol, instrument.kite_token FROM instrument \
             WHERE instrument.kite_token IS NOT NULL ORDER BY instrument.id LIMIT $1",
        )
        .bind(i64::try_from(PROBE_SYMBOLS.len()).unwrap_or(5))
        .fetch_all(&mut *conn)
        .await?;
    }
    Ok(rows)
}

/// What the probe reads (`ProbeSource`): production, the Kite provider; tests, a scripted fake.
/// An error is carried as `str(exc)`, which is all the report keeps of it.
pub(super) trait ProbeSource {
    /// `quotes(symbols)`.
    async fn quotes(&self, symbols: &[&str]) -> Result<Vec<QuoteRecord>, String>;
    /// The minute candles' start times for `token` over `[start, end]`.
    async fn minute_candles(
        &self,
        token: i64,
        start: NaiveDateTime,
        end: NaiveDateTime,
    ) -> Result<Vec<NaiveDateTime>, String>;
}

impl ProbeSource for KiteProvider {
    async fn quotes(&self, symbols: &[&str]) -> Result<Vec<QuoteRecord>, String> {
        KiteProvider::quotes(self, symbols, "NSE")
            .await
            .map_err(|e| e.to_string())
    }

    async fn minute_candles(
        &self,
        token: i64,
        start: NaiveDateTime,
        end: NaiveDateTime,
    ) -> Result<Vec<NaiveDateTime>, String> {
        let bars = self
            .minute_bars(token, start, end)
            .await
            .map_err(|e| e.to_string())?;
        Ok(bars
            .iter()
            .filter_map(|b| b.ts.aware().map(|a| a.naive_local()))
            .collect())
    }
}

/// The probe's clock (`now` and `sleep`): production, the task's clock (which advances in real
/// time); tests, a driven clock.
pub(super) trait ProbeClock {
    /// `now()`, IST wall clock.
    fn now(&self) -> NaiveDateTime;
    /// `sleep(seconds)`.
    async fn sleep(&self, wait: chrono::Duration);
}

impl ProbeClock for TaskContext {
    fn now(&self) -> NaiveDateTime {
        self.now_ist()
    }

    async fn sleep(&self, wait: chrono::Duration) {
        let millis = u64::try_from(wait.num_milliseconds()).unwrap_or(0);
        tokio::time::sleep(std::time::Duration::from_millis(millis)).await;
    }
}

/// `run_timing_probe`: walk the schedule against the clock. A sample already in the past when
/// the probe reaches it is skipped (and listed); a source error is recorded and the walk goes on.
async fn run_timing_probe<S: ProbeSource, C: ProbeClock>(
    source: &S,
    clock: &C,
    symbols: &[String],
    token: i64,
    day: NaiveDate,
) -> ProbeReport {
    let mut report = ProbeReport {
        day,
        symbols: symbols.to_vec(),
        token,
        quotes: Vec::new(),
        candles: Vec::new(),
        skipped: Vec::new(),
        errors: Vec::new(),
    };
    for (when, kind) in schedule() {
        let target = day.and_time(when);
        let moment = clock.now();
        if moment > target + chrono::Duration::seconds(20) {
            report.skipped.push(format!(
                "{} at {} (probe reached it at {})",
                kind.name(),
                time_iso(when),
                moment.format("%H:%M:%S")
            ));
            continue;
        }
        let wait = target - moment;
        if wait > chrono::Duration::zero() {
            clock.sleep(wait).await;
        }
        let stamp = clock.now();
        match kind {
            Kind::Quote => {
                let wanted: Vec<&str> = symbols.iter().map(String::as_str).collect();
                match source.quotes(&wanted).await {
                    Err(e) => report
                        .errors
                        .push(format!("quote at {}: {e}", time_iso(when))),
                    Ok(rows) => {
                        for row in rows {
                            report.quotes.push(QuoteSample {
                                at: target,
                                symbol: row.symbol.clone(),
                                last_price: row.last_price,
                                open: row.open,
                                volume: row.volume,
                                as_of: row.as_of,
                            });
                        }
                    }
                }
            }
            Kind::Candle => {
                let start = day.and_time(t(9, 15, 0));
                match source.minute_candles(token, start, stamp).await {
                    Err(e) => report.candles.push(CandleSample {
                        at: target,
                        token,
                        count: 0,
                        last_start: None,
                        error: Some(e),
                    }),
                    Ok(starts) => report.candles.push(CandleSample {
                        at: target,
                        token,
                        count: i64::try_from(starts.len()).unwrap_or(i64::MAX),
                        last_start: starts.iter().max().copied(),
                        error: None,
                    }),
                }
            }
        }
    }
    report
}

fn yes_no(value: Option<bool>) -> &'static str {
    match value {
        None => "unknown (no sample)",
        Some(true) => "**yes**",
        Some(false) => "**no**",
    }
}

/// `render_report`.
fn render_report(report: &ProbeReport, generated_at: NaiveDateTime) -> String {
    let seen = report.candle_first_seen_at();
    let mut lines: Vec<String> = vec![
        "# S2 — Kite timing, as measured".to_owned(),
        String::new(),
        format!(
            "Probed on {} (IST), written {} IST by `baskfy.swing.timing_probe` \
             (`docs/swing/STANDING-ANSWERS.md` A4). Symbols: {}; minute candles for token {}.",
            report.day.format("%Y-%m-%d"),
            generated_at.format("%Y-%m-%d %H:%M:%S"),
            report.symbols.join(", "),
            report.token
        ),
        String::new(),
        format!(
            "Good run: {}",
            if report.good() {
                "yes."
            } else {
                "NO — see skipped/errors; the Beat entry stays armed."
            }
        ),
        String::new(),
        "## The four answers".to_owned(),
        String::new(),
        format!(
            "1. `volume` at ≤ 09:09 carries the pre-open matched quantity: {}",
            yes_no(report.preopen_volume_seen())
        ),
        format!(
            "2. The pre-open `last_price` moves during 09:00-09:09: {}",
            yes_no(report.preopen_ltp_moved())
        ),
        format!(
            "3. `ohlc.open` is populated before 09:15: {}",
            yes_no(report.open_before_session())
        ),
        format!(
            "4. The 09:20 minute candle first appeared in `historical_data` at: {}",
            seen.map_or_else(
                || "never in the three asks".to_owned(),
                |s| s.format("%H:%M:%S").to_string()
            )
        ),
        String::new(),
        "## What this means for the schedule".to_owned(),
        String::new(),
        format!(
            "- Gap scan: {}",
            if report.preopen_volume_seen() == Some(true)
                && report.open_before_session() == Some(true)
            {
                "answers 1 and 3 are both yes, so the scan could return to 09:09 (one line in \
                 `celery_app.py`, `swing-premarket-gaps`); it stays at 09:16 until Maulik moves it."
            } else {
                "at least one of answers 1 and 3 is no or unknown — the scan stays at 09:16 \
                 (SW11, MD5)."
            }
        ),
        format!(
            "- Opening range: built from the ticks either way (A4); \
             `range_reconcile_delay_minutes` {}",
            match seen {
                Some(at) if at.time() <= t(9, 21, 5) => format!(
                    "[1] is enough — the candle was there at {}.",
                    at.format("%H:%M:%S")
                ),
                _ =>
                    "[1] may be short — the candle was not seen by 09:21:05; consider 2.".to_owned(),
            }
        ),
        String::new(),
        "## Quote samples".to_owned(),
        String::new(),
        "| at | symbol | last_price | ohlc.open | volume | exchange timestamp |".to_owned(),
        "|---|---|---|---|---|---|".to_owned(),
    ];
    for q in &report.quotes {
        lines.push(format!(
            "| {} | {} | {} | {} | {} | {} |",
            q.at.format("%H:%M:%S"),
            q.symbol,
            q.last_price,
            q.open.map_or_else(|| "-".to_owned(), |o| o.to_string()),
            q.volume,
            q.as_of.as_ref().map_or_else(|| "-".to_owned(), as_of_clock)
        ));
    }
    lines.extend([
        String::new(),
        "## Minute-candle samples".to_owned(),
        String::new(),
        "| asked at | candles | last candle start | error |".to_owned(),
        "|---|---|---|---|".to_owned(),
    ]);
    for c in &report.candles {
        lines.push(format!(
            "| {} | {} | {} | {} |",
            c.at.format("%H:%M:%S"),
            c.count,
            c.last_start
                .map_or_else(|| "-".to_owned(), |l| l.format("%H:%M").to_string()),
            c.error.clone().unwrap_or_default()
        ));
    }
    if !report.skipped.is_empty() {
        lines.extend([
            String::new(),
            "## Skipped (the probe started late)".to_owned(),
            String::new(),
        ]);
        lines.extend(report.skipped.iter().map(|x| format!("- {x}")));
    }
    if !report.errors.is_empty() {
        lines.extend([String::new(), "## Errors".to_owned(), String::new()]);
        lines.extend(report.errors.iter().map(|x| format!("- {x}")));
    }
    lines.push(String::new());
    lines.join("\n")
}

fn os_error(e: &std::io::Error) -> WorkerError {
    let class = match e.kind() {
        std::io::ErrorKind::PermissionDenied => "PermissionError",
        std::io::ErrorKind::NotFound => "FileNotFoundError",
        _ => "OSError",
    };
    WorkerError::python(class, e.to_string())
}

/// `probe_once`: refuse if the marker exists, sample, write the report, and — after a good run
/// only — write the marker.
pub(super) async fn probe_once<S: ProbeSource, C: ProbeClock>(
    source: &S,
    clock: &C,
    out_dir: &Path,
    symbols: &[String],
    token: i64,
) -> Result<OrderedJson, WorkerError> {
    std::fs::create_dir_all(out_dir).map_err(|e| os_error(&e))?;
    let marker = out_dir.join(DONE_MARKER);
    if marker.exists() {
        return Ok(object([
            (
                "skipped",
                s(format!(
                    "{} exists — the probe already had its good run",
                    marker.display()
                )),
            ),
            ("good", OrderedJson::Bool(true)),
        ]));
    }
    let day = clock.now().date();
    let report = run_timing_probe(source, clock, symbols, token, day).await;
    let text = render_report(&report, clock.now());
    let path = out_dir.join(REPORT_NAME);
    std::fs::write(&path, text).map_err(|e| os_error(&e))?;
    if report.good() {
        std::fs::write(&marker, format!("{}\n", day.format("%Y-%m-%d")))
            .map_err(|e| os_error(&e))?;
    }
    let mut out = vec![("report".to_owned(), s(path.display().to_string()))];
    out.extend(report.as_detail());
    Ok(OrderedJson::Object(out))
}

#[cfg(test)]
mod tests {
    use std::cell::{Cell, RefCell};

    use baskfy_providers::records::QuoteFields;

    use super::*;

    /// A Thursday.
    fn day() -> NaiveDate {
        NaiveDate::from_ymd_opt(2026, 9, 3).unwrap_or_default()
    }

    fn at(h: u32, m: u32, sec: u32) -> NaiveDateTime {
        day().and_time(t(h, m, sec))
    }

    fn dec(text: &str) -> Decimal {
        text.parse().unwrap_or_default()
    }

    /// A driven clock: `now()` reads it, `sleep()` advances it.
    struct Clock(Cell<NaiveDateTime>);

    impl ProbeClock for Clock {
        fn now(&self) -> NaiveDateTime {
            self.0.get()
        }

        async fn sleep(&self, wait: chrono::Duration) {
            self.0.set(self.0.get() + wait);
        }
    }

    /// One Kite morning: the pre-open LTP moves at 09:07:30, `ohlc.open` and `volume` appear at
    /// `open_at`, the 09:20 candle is served from `candle_from`; or nothing before the open.
    struct ScriptedKite<'a> {
        clock: &'a Clock,
        open_at: NaiveTime,
        candle_from: NaiveTime,
        nothing_before_open: bool,
        first_candle_fails: Cell<bool>,
        quote_calls: RefCell<Vec<NaiveDateTime>>,
        candle_calls: Cell<usize>,
    }

    impl<'a> ScriptedKite<'a> {
        fn new(clock: &'a Clock) -> Self {
            Self {
                clock,
                open_at: t(9, 8, 0),
                candle_from: t(9, 20, 35),
                nothing_before_open: false,
                first_candle_fails: Cell::new(false),
                quote_calls: RefCell::new(Vec::new()),
                candle_calls: Cell::new(0),
            }
        }
    }

    fn record(symbol: &str, last: &str, volume: i64, open: Option<&str>) -> QuoteRecord {
        let mut f = QuoteFields::new(symbol, dec(last));
        f.volume = volume;
        f.prev_close = Some(dec("100.00"));
        f.open = open.map(dec);
        QuoteRecord::new(f).unwrap_or_else(|_| unreachable!())
    }

    impl ProbeSource for ScriptedKite<'_> {
        async fn quotes(&self, symbols: &[&str]) -> Result<Vec<QuoteRecord>, String> {
            let now = self.clock.now();
            self.quote_calls.borrow_mut().push(now);
            if self.nothing_before_open {
                let before = now.time() < t(9, 15, 0);
                return Ok(symbols
                    .iter()
                    .map(|s| {
                        if before {
                            record(s, "100", 0, None)
                        } else {
                            record(s, "103", 500, Some("102"))
                        }
                    })
                    .collect());
            }
            let moved = now.time() >= t(9, 7, 30);
            let opened = now.time() >= self.open_at;
            Ok(symbols
                .iter()
                .map(|s| {
                    let mut r = record(
                        s,
                        if moved { "101.50" } else { "100.00" },
                        if opened { 12_000 } else { 0 },
                        opened.then_some("101.50"),
                    )
                    .into_fields();
                    r.as_of = Some(PyDateTime::Naive(now));
                    QuoteRecord::new(r).unwrap_or_else(|_| unreachable!())
                })
                .collect())
        }

        async fn minute_candles(
            &self,
            _token: i64,
            _start: NaiveDateTime,
            _end: NaiveDateTime,
        ) -> Result<Vec<NaiveDateTime>, String> {
            self.candle_calls.set(self.candle_calls.get() + 1);
            if self.first_candle_fails.replace(false) {
                return Err("token expired".to_owned());
            }
            let last = if self.clock.now().time() >= self.candle_from {
                t(9, 20, 0)
            } else {
                t(9, 19, 0)
            };
            let mut out = Vec::new();
            let mut minute = day().and_time(t(9, 15, 0));
            while minute.time() <= last {
                out.push(minute);
                minute += chrono::Duration::minutes(1);
            }
            Ok(out)
        }
    }

    fn block_on<F: std::future::Future>(f: F) -> F::Output {
        tokio::runtime::Builder::new_current_thread()
            .build()
            .map(|rt| rt.block_on(f))
            .unwrap_or_else(|_| unreachable!())
    }

    fn run(source: &ScriptedKite<'_>, clock: &Clock) -> ProbeReport {
        let symbols = vec!["RELIANCE".to_owned(), "TCS".to_owned()];
        block_on(run_timing_probe(source, clock, &symbols, 738_561, day()))
    }

    #[test]
    fn timing_probe_samples_every_scheduled_moment_in_order() {
        let clock = Clock(Cell::new(at(9, 4, 0)));
        let source = ScriptedKite::new(&clock);
        let report = run(&source, &clock);
        let quote_times: Vec<NaiveTime> = source
            .quote_calls
            .borrow()
            .iter()
            .map(NaiveDateTime::time)
            .collect();
        let scheduled: Vec<NaiveTime> = schedule()
            .iter()
            .filter(|(_, k)| *k == Kind::Quote)
            .map(|(w, _)| *w)
            .collect();
        assert_eq!(quote_times, scheduled);
        assert_eq!(source.candle_calls.get(), 3);
        assert_eq!(clock.now().time(), t(9, 21, 10));
        assert!(report.skipped.is_empty() && report.errors.is_empty());
        assert_eq!(report.quotes.len(), 2 * scheduled.len());
    }

    #[test]
    fn timing_probe_answers_the_four_questions_from_the_samples() {
        let clock = Clock(Cell::new(at(9, 4, 0)));
        let report = run(&ScriptedKite::new(&clock), &clock);
        assert_eq!(report.preopen_volume_seen(), Some(true));
        assert_eq!(report.preopen_ltp_moved(), Some(true));
        assert_eq!(report.open_before_session(), Some(true));
        assert_eq!(report.candle_first_seen_at(), Some(at(9, 20, 35)));
        assert!(report.good());
    }

    #[test]
    fn timing_probe_reads_the_other_hypothesis_honestly() {
        let clock = Clock(Cell::new(at(9, 4, 0)));
        let mut source = ScriptedKite::new(&clock);
        source.nothing_before_open = true;
        source.open_at = t(9, 15, 0);
        source.candle_from = t(9, 21, 5);
        let report = run(&source, &clock);
        assert_eq!(report.preopen_volume_seen(), Some(false));
        assert_eq!(report.preopen_ltp_moved(), Some(false));
        assert_eq!(report.open_before_session(), Some(false));
        assert_eq!(report.candle_first_seen_at(), Some(at(9, 21, 5)));
        assert!(report.good());
    }

    #[test]
    fn timing_probe_started_late_skips_what_it_missed_and_is_not_a_good_run() {
        let clock = Clock(Cell::new(at(9, 18, 0)));
        let report = run(&ScriptedKite::new(&clock), &clock);
        assert_eq!(report.skipped.len(), 9);
        assert!(report.skipped[0].starts_with("quote at 09:04:30"));
        assert_eq!(
            report.quotes.first().map(|q| q.at.time()),
            Some(t(9, 21, 10))
        );
        assert_eq!(report.preopen_volume_seen(), None);
        assert!(!report.good());
    }

    #[test]
    fn timing_probe_records_a_source_error_and_carries_on() {
        let clock = Clock(Cell::new(at(9, 4, 0)));
        let source = ScriptedKite::new(&clock);
        source.first_candle_fails.set(true);
        let report = run(&source, &clock);
        assert_eq!(report.candles[0].error.as_deref(), Some("token expired"));
        assert_eq!(report.candles[1].error, None);
        assert!(!report.good());
    }

    #[test]
    fn timing_probe_report_names_the_answers_and_the_schedule_consequence() {
        let clock = Clock(Cell::new(at(9, 4, 0)));
        let report = run(&ScriptedKite::new(&clock), &clock);
        let text = render_report(&report, clock.now());
        assert!(text.starts_with("# S2 — Kite timing, as measured"));
        assert!(
            text.contains("1. `volume` at ≤ 09:09 carries the pre-open matched quantity: **yes**")
        );
        assert!(text.contains("3. `ohlc.open` is populated before 09:15: **yes**"));
        assert!(text.contains("first appeared in `historical_data` at: 09:20:35"));
        assert!(text.contains("the scan could return to 09:09"));
        assert!(text.contains("| 09:09:00 | RELIANCE | 101.50 | 101.50 | 12000 |"));
    }

    fn scratch(name: &str) -> std::path::PathBuf {
        std::env::temp_dir().join(format!("baskfy-probe-{name}-{}", std::process::id()))
    }

    #[test]
    fn timing_probe_writes_the_report_and_a_marker_after_one_good_run() {
        let dir = scratch("good").join("status");
        let _ = std::fs::remove_dir_all(&dir);
        let clock = Clock(Cell::new(at(9, 4, 0)));
        let source = ScriptedKite::new(&clock);
        let symbols = vec!["RELIANCE".to_owned()];
        let result =
            block_on(probe_once(&source, &clock, &dir, &symbols, 1)).unwrap_or(OrderedJson::Null);
        assert_eq!(result.get("good"), Some(&OrderedJson::Bool(true)));
        let report = std::fs::read_to_string(dir.join(REPORT_NAME)).unwrap_or_default();
        assert!(report.starts_with("# S2"));
        let marker = std::fs::read_to_string(dir.join(DONE_MARKER)).unwrap_or_default();
        assert_eq!(marker.trim(), "2026-09-03");
        // The second morning: nothing is sampled.
        let again_clock = Clock(Cell::new(at(9, 4, 0)));
        let again_source = ScriptedKite::new(&again_clock);
        let again = block_on(probe_once(&again_source, &again_clock, &dir, &symbols, 1))
            .unwrap_or(OrderedJson::Null);
        assert!(matches!(
            again.get("skipped"),
            Some(OrderedJson::Str(s)) if s.contains("already had its good run")
        ));
        assert!(again_source.quote_calls.borrow().is_empty());
        let _ = std::fs::remove_dir_all(scratch("good"));
    }

    #[test]
    fn timing_probe_does_not_disable_itself_after_a_bad_run() {
        let dir = scratch("bad");
        let _ = std::fs::remove_dir_all(&dir);
        let clock = Clock(Cell::new(at(9, 18, 0)));
        let source = ScriptedKite::new(&clock);
        let result = block_on(probe_once(
            &source,
            &clock,
            &dir,
            &["RELIANCE".to_owned()],
            1,
        ))
        .unwrap_or(OrderedJson::Null);
        assert_eq!(result.get("good"), Some(&OrderedJson::Bool(false)));
        assert!(dir.join(REPORT_NAME).exists() && !dir.join(DONE_MARKER).exists());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn the_schedule_is_sorted_and_asks_for_three_candles() {
        let s = schedule();
        assert!(s.windows(2).all(|w| w[0].0 < w[1].0));
        assert_eq!(
            s.iter().filter(|(_, k)| *k == Kind::Candle).count(),
            CANDLE_ASKS
        );
    }
}
