//! `scripts/autorun.py`: run whatever today still needs collected (`baskfy-desk service
//! autorun [--check] [--timeout N]`). Collection only, never an order.

use std::collections::BTreeSet;
use std::io::Read as _;
use std::path::Path;
use std::time::{Duration as StdDuration, Instant};

use chrono::{Datelike, Duration, NaiveDate, Weekday};

use crate::app::AppState;
use crate::lanes::analytics::analytics_views::autorun as ar;
use crate::lanes::analytics::index_cache::normalise_session_date;
use crate::lanes::service_loop::{child_argv, program, say};
use crate::lanes::weekly::analyze::dumps_indent2;
use crate::wire::{PyDict, PyValue, dict};

/// `ARGV["daily"]` after the program.
pub const DAILY: [&str; 4] = ["daily", "--quiet", "--source", "page"];

/// NIFTY's index token (`instruments.REGISTRY["nifty"]`).
pub const NIFTY_TOKEN: i64 = 256_265;

/// `strangle.yaml`, whose `session.extra_holidays` the calendar adds.
pub const STRANGLE_CONFIG: &str = "config/strangle.yaml";

fn weekday_num(d: NaiveDate) -> u32 {
    d.weekday().num_days_from_monday()
}

/// `holidays_from_index(rows)`: weekdays in the covered span with no candle.
#[must_use]
pub fn holidays_from_index(traded: &BTreeSet<NaiveDate>) -> BTreeSet<NaiveDate> {
    let (Some(start), Some(end)) = (traded.first(), traded.last()) else {
        return BTreeSet::new();
    };
    let mut out = BTreeSet::new();
    let mut day = *start;
    while day <= *end {
        if weekday_num(day) < 5 && !traded.contains(&day) {
            out.insert(day);
        }
        day += Duration::days(1);
    }
    out
}

/// `holidays_from_expiries(expiries, weekday=TUESDAY)`.
#[must_use]
pub fn holidays_from_expiries(expiries: &BTreeSet<NaiveDate>) -> BTreeSet<NaiveDate> {
    let tuesday = Weekday::Tue.num_days_from_monday();
    let mut out = BTreeSet::new();
    for e in expiries {
        if weekday_num(*e) == tuesday {
            continue;
        }
        let mut probe = *e + Duration::days(1);
        for _ in 0..6 {
            if weekday_num(probe) == tuesday {
                out.insert(probe);
                break;
            }
            if weekday_num(probe) < 5 {
                out.insert(probe);
            }
            probe += Duration::days(1);
        }
    }
    out
}

/// `Calendar.is_trading_day(day)` over the derived holidays.
#[must_use]
pub fn is_trading_day(holidays: &BTreeSet<NaiveDate>, day: NaiveDate) -> bool {
    weekday_num(day) < 5 && !holidays.contains(&day)
}

/// `SC.load(config)["session"].get("extra_holidays") or ()`, for the file's flat list forms.
///
/// # Errors
/// The file cannot be read, or a listed date does not parse.
pub fn extra_holidays(path: &Path) -> Result<Vec<NaiveDate>, String> {
    let text = std::fs::read_to_string(path)
        .map_err(|e| format!("[Errno 2] {e}: '{}'", path.display()))?;
    let parse = |s: &str| {
        let s = s.trim().trim_matches(|c| c == '"' || c == '\'');
        NaiveDate::parse_from_str(s, "%Y-%m-%d")
            .map_err(|_| format!("Invalid isoformat string: '{s}'"))
    };
    let lines: Vec<&str> = text.lines().collect();
    for (i, line) in lines.iter().enumerate() {
        let body = line.split('#').next().unwrap_or("");
        let Some(rest) = body.trim_start().strip_prefix("extra_holidays:") else {
            continue;
        };
        let rest = rest.trim();
        if let Some(inner) = rest.strip_prefix('[') {
            let inner = inner.trim_end_matches(']');
            return inner
                .split(',')
                .filter(|s| !s.trim().is_empty())
                .map(parse)
                .collect();
        }
        if rest.is_empty() || rest == "null" || rest == "~" {
            let mut out = Vec::new();
            for next in &lines[i + 1..] {
                let t = next.split('#').next().unwrap_or("").trim();
                if t.is_empty() {
                    continue;
                }
                let Some(item) = t.strip_prefix("- ") else {
                    break;
                };
                out.push(parse(item)?);
            }
            return Ok(out);
        }
        return Ok(vec![parse(rest)?]);
    }
    Ok(Vec::new())
}

async fn trading_today(state: &AppState, today: NaiveDate) -> Result<bool, String> {
    let kite = crate::lanes::exec_core::exec_kite::kite(state).map_err(|e| e.message)?;
    let from = (today - Duration::days(400)).format("%Y-%m-%d").to_string();
    let to = today.format("%Y-%m-%d").to_string();
    let rows = kite
        .historical(NIFTY_TOKEN, &from, &to, "day")
        .await
        .map_err(|e| e.message)?;
    let dump = kite.instruments("NFO").await.map_err(|e| e.message)?;
    let field = |v: &PyValue, k: &str| match v {
        PyValue::Map(m) => m.get(k).cloned(),
        _ => None,
    };
    let mut traded = BTreeSet::new();
    for r in &rows {
        traded.insert(normalise_session_date(
            &field(r, "date").unwrap_or(PyValue::None),
        )?);
    }
    let mut expiries = BTreeSet::new();
    for i in &dump {
        if field(i, "name") != Some(PyValue::Str("NIFTY".to_owned())) {
            continue;
        }
        match field(i, "expiry") {
            None | Some(PyValue::None) => {}
            Some(PyValue::Str(s)) if s.is_empty() => {}
            Some(v) => {
                expiries.insert(normalise_session_date(&v)?);
            }
        }
    }
    let extra = extra_holidays(&state.paths.root.join(STRANGLE_CONFIG))?;
    let mut holidays = holidays_from_index(&traded);
    holidays.extend(holidays_from_expiries(&expiries));
    holidays.extend(extra);
    Ok(is_trading_day(&holidays, today))
}

/// `subprocess.run(argv, capture_output=True, timeout=)`: `Some((code, stdout, stderr))`, or
/// `None` after killing a child that ran past `timeout`.
fn run_captured(argv: &[String], timeout: u64) -> Result<Option<(i32, String, String)>, String> {
    let (prog, args) = argv.split_first().ok_or("empty argv")?;
    let mut child = std::process::Command::new(prog)
        .args(args)
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .map_err(|e| e.to_string())?;
    let reader = |pipe: Option<Box<dyn std::io::Read + Send>>| {
        std::thread::spawn(move || {
            let mut s = String::new();
            if let Some(mut p) = pipe {
                let mut buf = Vec::new();
                let _ = p.read_to_end(&mut buf);
                s = String::from_utf8_lossy(&buf).into_owned();
            }
            s
        })
    };
    let out = reader(child.stdout.take().map(|p| Box::new(p) as Box<dyn std::io::Read + Send>));
    let err = reader(child.stderr.take().map(|p| Box::new(p) as Box<dyn std::io::Read + Send>));
    let deadline = Instant::now() + StdDuration::from_secs(timeout);
    let status = loop {
        if let Some(s) = child.try_wait().map_err(|e| e.to_string())? {
            break Some(s);
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            break None;
        }
        std::thread::sleep(StdDuration::from_millis(100));
    };
    let stdout = out.join().unwrap_or_default();
    let stderr = err.join().unwrap_or_default();
    Ok(status.map(|s| (s.code().unwrap_or(1), stdout, stderr)))
}

fn tail(stdout: &str, stderr: &str) -> String {
    let s = if stdout.is_empty() { stderr } else { stdout }.trim();
    let n = s.chars().count();
    s.chars().skip(n.saturating_sub(400)).collect()
}

fn str_v(s: &str) -> PyValue {
    PyValue::Str(s.to_owned())
}

/// Parsed `--check` / `--timeout`.
///
/// # Errors
/// An unknown flag or a non-integer timeout.
pub fn parse_args(argv: &[String]) -> Result<(bool, u64), String> {
    let usage = "usage: autorun.py [-h] [--check] [--timeout TIMEOUT]\nautorun.py: error:";
    let (mut check, mut timeout) = (false, 900u64);
    let mut it = argv.iter();
    while let Some(a) = it.next() {
        match a.as_str() {
            "--check" => check = true,
            "--timeout" => {
                let v = it
                    .next()
                    .ok_or_else(|| format!("{usage} argument --timeout: expected one argument"))?;
                timeout = v
                    .parse()
                    .map_err(|_| format!("{usage} argument --timeout: invalid int value: '{v}'"))?;
            }
            other => return Err(format!("{usage} unrecognized arguments: {other}")),
        }
    }
    Ok((check, timeout))
}

/// `baskfy-desk service autorun [args]`: `main()`'s exit code.
pub async fn main(state: &AppState, argv: &[String]) -> i32 {
    let (check, timeout) = match parse_args(argv) {
        Ok(v) => v,
        Err(e) => {
            #[allow(clippy::print_stderr)]
            {
                eprintln!("{e}");
            }
            return 2;
        }
    };
    let now = state.clock.now_ist().naive_local();
    let kite = match crate::lanes::exec_core::exec_kite::kite(state) {
        Ok(k) => k,
        Err(e) => {
            say(&format!("{}: {}", e.python_type, e.message));
            return 1;
        }
    };
    if !kite.is_authed().await {
        let body = PyValue::Map(dict([
            ("status", str_v("AUTH_REQUIRED")),
            ("login_url", str_v(&kite.login_url().unwrap_or_default())),
        ]));
        say(&dumps_indent2(&body));
        return 2;
    }
    let trading = match trading_today(state, now.date()).await {
        Ok(t) => t,
        Err(e) => {
            let body = PyValue::Map(dict([
                ("status", str_v("CALENDAR_UNAVAILABLE")),
                ("error", str_v(&e.chars().take(200).collect::<String>())),
            ]));
            say(&dumps_indent2(&body));
            return 1;
        }
    };
    let items = match state.db.connect().await {
        Ok(mut conn) => {
            if let Err(e) = conn.migrate().await {
                say(&e.to_string());
                return 1;
            }
            match ar::needed(&mut conn, now, trading, state.settings_snapshot().options_enabled)
                .await
            {
                Ok(i) => i,
                Err(e) => {
                    say(&e.to_string());
                    return 1;
                }
            }
        }
        Err(e) => {
            say(&e.to_string());
            return 1;
        }
    };
    let mut head: PyDict = dict([
        ("session", str_v(&now.date().format("%Y-%m-%d").to_string())),
        ("trading_day", PyValue::Bool(trading)),
        (
            "outstanding",
            PyValue::List(
                items
                    .iter()
                    .map(|i| i.get("op").cloned().unwrap_or(PyValue::None))
                    .collect(),
            ),
        ),
        ("summary", str_v(&ar::summary(&items))),
        (
            "detail",
            PyValue::List(items.iter().cloned().map(PyValue::Map).collect()),
        ),
    ]);
    if check || items.is_empty() {
        head.insert(
            "status",
            str_v(if items.is_empty() { "NOTHING_DUE" } else { "CHECKED" }),
        );
        say(&dumps_indent2(&PyValue::Map(head)));
        return 0;
    }
    let prog = program();
    let mut ran = Vec::new();
    for item in &items {
        let g = |k: &str| item.get(k).cloned().unwrap_or(PyValue::None);
        if g("op").py_str() != "daily" {
            ran.push(PyValue::Map(dict([
                ("op", g("op")),
                ("label", g("label")),
                ("ok", PyValue::Bool(false)),
                ("tail", str_v("the options lab is frozen; nothing to run")),
            ])));
            continue;
        }
        let argv = child_argv(&prog, DAILY[0], &DAILY[1..]);
        let res = tokio::task::spawn_blocking(move || run_captured(&argv, timeout))
            .await
            .map_err(|e| e.to_string())
            .and_then(|r| r);
        let row = match res {
            Ok(Some((code, out, err))) => dict([
                ("op", g("op")),
                ("label", g("label")),
                ("instrument", g("instrument")),
                ("code", PyValue::Int(i64::from(code))),
                ("ok", PyValue::Bool(code == 0)),
                ("tail", str_v(&tail(&out, &err))),
            ]),
            Ok(None) => dict([
                ("op", g("op")),
                ("label", g("label")),
                ("code", PyValue::None),
                ("ok", PyValue::Bool(false)),
                ("tail", str_v(&format!("exceeded {timeout}s"))),
            ]),
            Err(e) => dict([
                ("op", g("op")),
                ("label", g("label")),
                ("code", PyValue::None),
                ("ok", PyValue::Bool(false)),
                ("tail", str_v(&e.chars().take(200).collect::<String>())),
            ]),
        };
        ran.push(PyValue::Map(row));
    }
    let ok = ran.iter().all(|r| match r {
        PyValue::Map(m) => m.get("ok") == Some(&PyValue::Bool(true)),
        _ => false,
    });
    head.insert("status", str_v(if ok { "OK" } else { "PARTIAL" }));
    head.insert("ran", PyValue::List(ran));
    say(&dumps_indent2(&PyValue::Map(head)));
    i32::from(!ok)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
    use super::*;

    fn d(m: u32, day: u32) -> NaiveDate {
        NaiveDate::from_ymd_opt(2026, m, day).unwrap()
    }

    #[test]
    fn a_monday_expiry_marks_the_tuesday_a_holiday() {
        let h = holidays_from_expiries(&[d(9, 28), d(10, 6)].into_iter().collect());
        assert_eq!(h, [d(9, 29)].into_iter().collect());
        assert!(!is_trading_day(&h, d(9, 29)));
        assert!(is_trading_day(&h, d(9, 30)));
        assert!(!is_trading_day(&h, d(10, 3)));
    }

    #[test]
    fn a_weekday_without_a_candle_is_a_holiday() {
        let traded = [d(10, 1), d(10, 5)].into_iter().collect();
        assert_eq!(
            holidays_from_index(&traded),
            [d(10, 2)].into_iter().collect()
        );
    }

    #[test]
    fn extra_holidays_reads_both_list_forms() {
        let dir = std::env::temp_dir().join(format!("autorun-cli-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("s.yaml");
        std::fs::write(&p, "session:\n  extra_holidays: []  # none\n").unwrap();
        assert!(extra_holidays(&p).unwrap().is_empty());
        std::fs::write(&p, "session:\n  extra_holidays:\n    - 2026-11-09\n    - '2026-11-10'\ntiming:\n").unwrap();
        assert_eq!(extra_holidays(&p).unwrap(), vec![d(11, 9), d(11, 10)]);
        assert!(extra_holidays(&dir.join("missing.yaml")).is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn args_parse() {
        assert_eq!(parse_args(&[]).unwrap(), (false, 900));
        let a: Vec<String> = ["--check", "--timeout", "5"].iter().map(|s| (*s).to_owned()).collect();
        assert_eq!(parse_args(&a).unwrap(), (true, 5));
        assert!(parse_args(&["--nope".to_owned()]).is_err());
    }
}
