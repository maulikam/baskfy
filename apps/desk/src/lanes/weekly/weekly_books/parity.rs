//! The sub-lane's parity gate: `fixtures/weekly_books/goldens.json`, Python's own answers
//! (`tools/parity/weekly_books_goldens.py`, never edited by hand), reproduced byte for byte as
//! `json.dumps` text. These modules have no route of their own, so this stands where a
//! `desk_diff.py` scenario would.
#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::float_cmp
)]

use std::collections::HashMap;

use chrono::{NaiveDate, NaiveDateTime};

use super::holdings_view::{self as hv, Context, FillRef};
use super::reconcile::{self as rc, HookError, MemoryIssueStore, OrderBook, SleeveHooks};
use super::tradebook::{self as tb, Action, Fill};
use super::{pyf, pyi, pys, reconcile_view as rv, snapshot as sn, tax_lots as tl};
use crate::wire::{PyDict, PyValue, dict, py_json_dumps};

const GOLDENS: &str = include_str!("../../../../fixtures/weekly_books/goldens.json");

fn m(v: &PyValue) -> &PyDict {
    match v {
        PyValue::Map(m) => m,
        other => panic!("not a dict: {other:?}"),
    }
}

fn l(v: &PyValue) -> &[PyValue] {
    match v {
        PyValue::List(l) => l,
        other => panic!("not a list: {other:?}"),
    }
}

fn g<'a>(d: &'a PyDict, k: &str) -> &'a PyValue {
    d.get(k).unwrap_or_else(|| panic!("missing {k}"))
}

fn dicts(v: &PyValue) -> Vec<PyDict> {
    l(v).iter().map(|x| m(x).clone()).collect()
}

fn naive(s: &str) -> NaiveDateTime {
    NaiveDateTime::parse_from_str(s, "%Y-%m-%dT%H:%M:%S%.f").unwrap()
}

fn date(s: &str) -> NaiveDate {
    NaiveDate::parse_from_str(s, "%Y-%m-%d").unwrap()
}

fn text(v: &PyValue) -> String {
    py_json_dumps(v).unwrap()
}

fn list(items: Vec<PyDict>) -> PyValue {
    PyValue::List(items.into_iter().map(PyValue::Map).collect())
}

fn holdings_case(input: &PyDict) -> PyValue {
    let holdings = dicts(g(input, "holdings"));
    let raw = dicts(g(input, "raw"));
    let scan: HashMap<String, PyDict> = m(g(input, "scan"))
        .iter()
        .map(|(k, v)| (k.to_owned(), m(v).clone()))
        .collect();
    let fills: Vec<FillRef> = dicts(g(input, "fills"))
        .iter()
        .map(|f| FillRef {
            symbol: pys(f.get("symbol")),
            side: pys(f.get("side")),
            when_ts: f.get("when_ts").map(|v| pyf(Some(v))),
        })
        .collect();
    let protected: HashMap<String, String> = m(g(input, "protected"))
        .iter()
        .map(|(k, v)| (k.to_owned(), v.py_str()))
        .collect();
    let cash = pyf(input.get("cash"));
    let ctx = Context {
        raw: &raw,
        scan: Some(&scan),
        fills: &fills,
        protected: Some(&protected),
    };
    let rows = hv::rows(&holdings, &ctx, naive(&pys(input.get("now")))).unwrap();
    let summary = hv::summary(&rows, cash).unwrap();
    PyValue::Map(dict([
        (
            "rows",
            list(rows.iter().map(hv::HoldingRow::to_py).collect()),
        ),
        ("summary", PyValue::Map(summary.dict.clone())),
        (
            "allocation",
            PyValue::Map(hv::allocation(&rows, cash).unwrap()),
        ),
        ("warnings", list(hv::warnings(&rows, &summary))),
    ]))
}

fn tax_case(input: &PyDict) -> PyValue {
    let lots = tl::lots_from_rows("X", &dicts(g(input, "lots"))).unwrap();
    let r = tl::review_sale(
        "X",
        pyi(input.get("qty")),
        pyf(input.get("price")),
        &lots,
        date(&pys(input.get("as_of"))),
        pyi(input.get("review_days")),
    );
    let items = tl::manual_review_items(&[("X".to_owned(), r.clone())]);
    PyValue::Map(dict([
        ("review", PyValue::Map(r.as_dict())),
        ("items", list(items)),
    ]))
}

fn fill_row(f: &Fill) -> PyValue {
    PyValue::List(vec![
        PyValue::Str(f.symbol.clone()),
        PyValue::Str(tb::stamp_iso(f.when, f.tz)),
        PyValue::Str(f.side.clone()),
        PyValue::Int(f.quantity),
        PyValue::Float(f.price),
        PyValue::Str(f.exchange.clone()),
        PyValue::Str(f.trade_id.clone()),
        PyValue::Float(f.charges),
    ])
}

fn parse_case(input: &PyDict, dir: &std::path::Path, n: usize) -> PyValue {
    let path = dir.join(format!("tb{n}.csv"));
    std::fs::write(&path, pys(input.get("csv"))).unwrap();
    match tb::parse_tradebook(&path) {
        Err(tb::ImportError::Uncaught(e)) => PyValue::Map(dict([("raised", PyValue::Str(e))])),
        Ok(fills) => PyValue::Map(dict([(
            "fills",
            PyValue::List(fills.iter().map(fill_row).collect()),
        )])),
        Err(e) => PyValue::Map(dict([(
            "error",
            PyValue::Str(e.to_string().replace(&path.display().to_string(), "<path>")),
        )])),
    }
}

/// `datetime.fromisoformat` for the goldens' stamps: naive, or with a `±HH:MM` offset.
fn stamp(text: &str) -> (NaiveDateTime, Option<chrono::FixedOffset>) {
    match chrono::DateTime::parse_from_str(text, "%Y-%m-%dT%H:%M:%S%.f%:z") {
        Ok(t) => (t.naive_local(), Some(*t.offset())),
        Err(_) => (naive(text), None),
    }
}

fn parse_fallback_case(input: &PyDict, dir: &std::path::Path, n: usize) -> PyValue {
    let path = dir.join(format!("fb{n}.csv"));
    std::fs::write(&path, pys(input.get("csv"))).unwrap();
    match tb::parse_tradebook(&path) {
        Err(tb::ImportError::Uncaught(e)) => PyValue::Map(dict([("raised", PyValue::Str(e))])),
        Err(e) => PyValue::Map(dict([(
            "error",
            PyValue::Str(e.to_string().replace(&path.display().to_string(), "<path>")),
        )])),
        Ok(fills) => PyValue::Map(dict([
            ("fills", PyValue::List(fills.iter().map(fill_row).collect())),
            (
                "keys",
                PyValue::List(
                    fills
                        .iter()
                        .map(|f| PyValue::Str(tb::fill_key(f)))
                        .collect(),
                ),
            ),
            (
                "ts",
                PyValue::List(fills.iter().map(|f| PyValue::Float(f.ts())).collect()),
            ),
        ])),
    }
}

fn fills_of(v: &PyValue) -> Vec<Fill> {
    l(v).iter()
        .map(|row| {
            let r = l(row);
            let (when, tz) = stamp(&r[1].py_str());
            Fill {
                symbol: r[0].py_str(),
                when,
                tz,
                side: r[2].py_str(),
                quantity: pyi(Some(&r[3])),
                price: pyf(Some(&r[4])),
                exchange: r[5].py_str(),
                trade_id: r[6].py_str(),
                charges: pyf(Some(&r[7])),
            }
        })
        .collect()
}

fn fifo_case(input: &PyDict) -> PyValue {
    let fills = fills_of(g(input, "fills"));
    let actions: Vec<Action> = l(g(input, "actions"))
        .iter()
        .map(|a| {
            let a = l(a);
            Action {
                symbol: a[0].py_str(),
                kind: a[1].py_str(),
                ex_date: date(&a[2].py_str()),
                ratio_new: pyf(Some(&a[3])),
                ratio_old: pyf(Some(&a[4])),
                note: String::new(),
            }
        })
        .collect();
    let r = tb::build_lots(&fills, &actions);
    let closed = r
        .closed
        .iter()
        .map(|c| {
            dict([
                ("symbol", PyValue::Str(c.symbol.clone())),
                ("entry_ts", PyValue::Float(c.entry_ts)),
                ("exit_ts", PyValue::Float(c.exit_ts)),
                ("qty", PyValue::Int(c.qty)),
                ("entry_price", PyValue::Float(c.entry_price)),
                ("exit_price", PyValue::Float(c.exit_price)),
                ("pnl", PyValue::Float(c.pnl)),
                ("costs", PyValue::Float(c.costs)),
            ])
        })
        .collect();
    let open = r
        .open_lots
        .iter()
        .map(|o| {
            PyValue::List(vec![
                PyValue::Str(o.symbol.clone()),
                PyValue::Str(tb::stamp_iso(o.when, o.tz)),
                PyValue::Int(o.quantity),
                PyValue::Float(o.price),
                PyValue::Float(o.charges),
                PyValue::Float(tb::stamp_ts(o.when, o.tz)),
            ])
        })
        .collect();
    let unmatched = r
        .unmatched_sells
        .iter()
        .map(|u| {
            dict([
                ("symbol", PyValue::Str(u.symbol.clone())),
                ("date", PyValue::Str(u.date.clone())),
                ("quantity", PyValue::Int(u.quantity)),
                ("price", PyValue::Float(u.price)),
            ])
        })
        .collect();
    PyValue::Map(dict([
        ("closed", list(closed)),
        ("open", PyValue::List(open)),
        ("unmatched", list(unmatched)),
        (
            "keys",
            PyValue::List(
                fills
                    .iter()
                    .map(|f| PyValue::Str(tb::fill_key(f)))
                    .collect(),
            ),
        ),
    ]))
}

fn rows_py(rows: &[crate::storage::Row]) -> PyValue {
    PyValue::List(
        rows.iter()
            .map(|r| PyValue::List(r.0.iter().map(|(_, v)| super::db_to_py(v)).collect()))
            .collect(),
    )
}

async fn tradebook_db_case(input: &PyDict, dir: &std::path::Path) -> PyValue {
    let now = naive(&pys(input.get("now")));
    let path = dir.join("tradebook.csv");
    std::fs::write(&path, pys(input.get("csv"))).unwrap();
    let mut c = crate::storage::Db::connect_sqlite_path(&dir.join("p.db"))
        .await
        .unwrap();
    c.migrate().await.unwrap();
    let mut imp = tb::import_tradebook(&mut c, &path, true, now)
        .await
        .unwrap();
    imp.insert("file", PyValue::Str("<path>".into()));
    let cap = tb::capture_live_trades(&mut c, l(g(input, "trades")), now)
        .await
        .unwrap();
    let st = tb::status(&mut c, &dicts(g(input, "positions")), now.date())
        .await
        .unwrap();
    let trades = c
        .execute(
            "SELECT symbol, entry_ts, exit_ts, qty, entry_price, exit_price, pnl, costs FROM trades ORDER BY id",
            &[],
        )
        .await
        .unwrap();
    let fills = c
        .execute(
            "SELECT trade_id, symbol, when_ts, side, quantity, price, exchange, charges, source, captured_at \
             FROM fills ORDER BY trade_id",
            &[],
        )
        .await
        .unwrap();
    PyValue::Map(dict([
        ("import", PyValue::Map(imp)),
        ("capture", PyValue::Map(cap)),
        ("status", PyValue::Map(st)),
        ("trades", rows_py(&trades.rows)),
        ("fills", rows_py(&fills.rows)),
    ]))
}

fn positions_case(input: &PyDict) -> PyValue {
    let plan = match g(input, "plan") {
        PyValue::Map(p) => Some(p.clone()),
        _ => None,
    };
    PyValue::Map(rv::positions_view(
        plan.as_ref(),
        &dicts(g(input, "holdings")),
    ))
}

struct Book(PyDict);

#[async_trait::async_trait]
impl OrderBook for Book {
    async fn orders(&self) -> Result<Vec<PyValue>, HookError> {
        Ok(l(g(&self.0, "orders")).to_vec())
    }
    async fn get_gtts(&self) -> Result<Vec<PyValue>, HookError> {
        Ok(l(g(&self.0, "gtts")).to_vec())
    }
    async fn holdings(&self) -> Result<Vec<PyDict>, HookError> {
        Ok(dicts(g(&self.0, "holdings")))
    }
}

struct Sleeve(PyDict, std::sync::Mutex<Vec<PyValue>>);

#[async_trait::async_trait]
impl SleeveHooks for Sleeve {
    fn name(&self) -> &str {
        "twt"
    }
    async fn open_orders(&self) -> Result<Vec<PyDict>, HookError> {
        Ok(dicts(g(&self.0, "sleeve_orders")))
    }
    async fn on_order_update(&self, payload: &PyDict) -> Result<Option<PyValue>, HookError> {
        Ok((pys(payload.get("status")) != "COMPLETE").then(|| PyValue::Str("applied".into())))
    }
    async fn open_positions(&self) -> Result<Vec<PyDict>, HookError> {
        Ok(dicts(g(&self.0, "positions")))
    }
    fn has_risk(&self) -> bool {
        true
    }
    async fn release(&self, symbol: &str, value: f64) -> Result<(), HookError> {
        self.1.lock().unwrap().push(PyValue::List(vec![
            PyValue::Str(symbol.to_owned()),
            PyValue::Float(value),
        ]));
        Ok(())
    }
}

async fn reconcile_case(input: &PyDict) -> PyValue {
    let at = chrono::DateTime::parse_from_rfc3339("2026-09-28T09:20:05+05:30").unwrap();
    let book = Book(input.clone());
    let sleeve = Sleeve(input.clone(), std::sync::Mutex::new(Vec::new()));
    let run =
        match rc::reconcile_once(&book, &[&sleeve], at, &mut MemoryIssueStore::default()).await {
            Ok(run) => run,
            // The pass raised: Python's case records `type(exc).__name__: exc` and nothing else.
            Err(e) => return PyValue::Map(dict([("raised", PyValue::Str(e))])),
        };
    let issues = run
        .issues
        .iter()
        .map(|i| {
            PyValue::List(vec![
                PyValue::Str(i.sleeve.clone()),
                PyValue::Int(i.position_id),
                PyValue::Str(i.symbol.clone()),
                PyValue::Str(i.kind.clone()),
                PyValue::Str(i.detail.clone()),
            ])
        })
        .collect();
    let mut idx: Vec<(String, rc::GttRow)> = rc::gtt_index(l(g(input, "gtts")))
        .unwrap()
        .into_iter()
        .collect();
    idx.sort_by(|a, b| a.0.cmp(&b.0));
    let gtts = dict(idx.into_iter().map(|(k, v)| {
        (
            k,
            PyValue::List(vec![
                PyValue::Str(v.gtt_id),
                PyValue::Str(v.status),
                PyValue::Str(v.symbol),
                v.quantity.map_or(PyValue::None, PyValue::Int),
                v.trigger
                    .map_or(PyValue::None, |t| PyValue::Str(t.to_string())),
            ]),
        )
    }));
    let released = sleeve.1.lock().unwrap().clone();
    PyValue::Map(dict([
        ("line", PyValue::Str(run.line())),
        ("released", PyValue::List(released)),
        ("issues", PyValue::List(issues)),
        ("gtts", PyValue::Map(gtts)),
    ]))
}

async fn reconcile_raise_case(input: &PyDict) -> PyValue {
    let at = chrono::DateTime::parse_from_rfc3339("2026-09-28T09:20:05+05:30").unwrap();
    let book = Book(input.clone());
    let sleeve = Sleeve(input.clone(), std::sync::Mutex::new(Vec::new()));
    match rc::reconcile_once(&book, &[&sleeve], at, &mut MemoryIssueStore::default()).await {
        Err(e) => PyValue::Map(dict([("raised", PyValue::Str(e))])),
        Ok(run) => PyValue::Map(dict([
            ("line", PyValue::Str(run.line())),
            ("released", PyValue::List(sleeve.1.lock().unwrap().clone())),
            (
                "issues",
                PyValue::List(
                    run.issues
                        .iter()
                        .map(|i| {
                            PyValue::List(vec![
                                PyValue::Str(i.sleeve.clone()),
                                PyValue::Int(i.position_id),
                                PyValue::Str(i.symbol.clone()),
                                PyValue::Str(i.kind.clone()),
                                PyValue::Str(i.detail.clone()),
                            ])
                        })
                        .collect(),
                ),
            ),
        ])),
    }
}

/// `_SourceKite`: holdings, quotes, the instrument dump and candles, every read recorded.
struct SourceKite {
    holdings: Vec<PyDict>,
    quotes: PyDict,
    instruments: Vec<PyValue>,
    candles: PyDict,
    calls: std::sync::Mutex<Vec<PyValue>>,
}

#[async_trait::async_trait]
impl sn::SnapshotKite for SourceKite {
    async fn is_authed(&self) -> bool {
        true
    }
    async fn holdings(&self) -> Result<Vec<PyDict>, String> {
        Ok(self.holdings.clone())
    }
    async fn available_cash(&self) -> Result<f64, String> {
        Ok(123_456.789)
    }
    async fn quote_raw(&self, keys: &[String]) -> Result<PyDict, String> {
        self.calls.lock().unwrap().push(PyValue::List(vec![
            PyValue::Str("quote_raw".into()),
            PyValue::List(keys.iter().map(|k| PyValue::Str(k.clone())).collect()),
        ]));
        Ok(dict(keys.iter().filter_map(|k| {
            self.quotes.get(k).map(|v| (k.clone(), v.clone()))
        })))
    }
    async fn instruments(&self, exchange: &str) -> Result<Vec<PyValue>, String> {
        self.calls.lock().unwrap().push(PyValue::List(vec![
            PyValue::Str("instruments".into()),
            PyValue::Str(exchange.to_owned()),
        ]));
        Ok(self.instruments.clone())
    }
    async fn historical(&self, token: i64, day: NaiveDate) -> Result<Vec<PyValue>, String> {
        let d = PyValue::Str(day.format("%Y-%m-%d").to_string());
        self.calls.lock().unwrap().push(PyValue::List(vec![
            PyValue::Str("historical".into()),
            PyValue::Int(token),
            d.clone(),
            d,
            PyValue::Str("day".into()),
        ]));
        Ok(match self.candles.get(&token.to_string()) {
            Some(PyValue::List(c)) => c.clone(),
            _ => Vec::new(),
        })
    }
}

async fn snapshot_source_case(input: &PyDict, dir: &std::path::Path) -> PyValue {
    let kite = SourceKite {
        holdings: dicts(g(input, "holdings")),
        quotes: m(g(input, "quotes")).clone(),
        instruments: l(g(input, "instruments")).to_vec(),
        candles: m(g(input, "candles")).clone(),
        calls: std::sync::Mutex::new(Vec::new()),
    };
    let on_date = match g(input, "on_date") {
        PyValue::Str(s) => Some(s.clone()),
        _ => None,
    };
    let source = pys(input.get("source"));
    let opts = sn::CollectOptions {
        on_date: on_date.as_deref(),
        price_source: &source,
        cash: match g(input, "cash") {
            PyValue::None => None,
            v => Some(pyf(Some(v))),
        },
        allow_stale_cash: false,
        today: date(&pys(input.get("today"))),
        include_untouchable_in_nav: false,
        root: dir,
    };
    let snapshot = match sn::collect(&kite, &opts).await {
        Ok(s) => PyValue::Map(dict([
            ("date", PyValue::Str(s.date)),
            ("nav", PyValue::Float(s.nav)),
            ("invested", PyValue::Float(s.invested)),
            ("cash", PyValue::Float(s.cash)),
            ("holdings_json", PyValue::Str(s.holdings_json)),
        ])),
        Err(sn::SnapshotError::Value(e)) => {
            PyValue::Map(dict([("raised", PyValue::Str(format!("ValueError: {e}")))]))
        }
        Err(e) => PyValue::Map(dict([(
            "raised",
            PyValue::Str(format!("RuntimeError: {e}")),
        )])),
    };
    let cache = std::fs::read(dir.join("data/.instruments_NSE.json"))
        .ok()
        .map_or(PyValue::None, |b| PyValue::from_json(&b).unwrap());
    let calls = kite.calls.lock().unwrap().clone();
    PyValue::Map(dict([
        ("snapshot", snapshot),
        ("calls", PyValue::List(calls)),
        ("cache", cache),
    ]))
}

async fn snapshot_cli_case(input: &PyDict, dir: &std::path::Path) -> PyValue {
    let kite = SnapKite(dicts(g(input, "holdings")));
    let ledger = dir.join("ledger.csv");
    std::fs::write(&ledger, pys(input.get("ledger"))).unwrap();
    let mut conn = crate::storage::Db::connect_sqlite_path(&dir.join("cli.db"))
        .await
        .unwrap();
    let mut runs = Vec::new();
    for argv in l(g(input, "runs")) {
        let argv: Vec<String> = l(argv)
            .iter()
            .map(|a| {
                let a = a.py_str();
                if a == "<ledger>" {
                    ledger.display().to_string()
                } else {
                    a
                }
            })
            .collect();
        let base = sn::CollectOptions {
            on_date: None,
            price_source: "holdings",
            cash: None,
            allow_stale_cash: false,
            today: date(&pys(input.get("today"))),
            include_untouchable_in_nav: false,
            root: dir,
        };
        let o = sn::cli(&argv, &kite, &mut conn, base, g(input, "dry_run").truthy()).await;
        runs.push(PyValue::Map(dict([
            ("code", PyValue::Int(i64::from(o.code))),
            (
                "stdout",
                PyValue::List(o.stdout.into_iter().map(PyValue::Str).collect()),
            ),
            (
                "message",
                if o.code == 1 {
                    PyValue::Str(o.stderr.join("\n"))
                } else {
                    PyValue::None
                },
            ),
        ])));
    }
    let rows = |sql: &str, cols: &[&str]| {
        let sql = sql.to_owned();
        let cols: Vec<String> = cols.iter().map(|c| (*c).to_owned()).collect();
        (sql, cols)
    };
    let mut tables = Vec::new();
    for (sql, cols) in [
        rows(
            "SELECT date, nav, invested, cash, index_value FROM snapshots ORDER BY date",
            &["date", "nav", "invested", "cash", "index_value"],
        ),
        rows(
            "SELECT date, amount, type FROM cashflows ORDER BY id",
            &["date", "amount", "type"],
        ),
    ] {
        let cur = conn.execute(&sql, &[]).await.unwrap();
        tables.push(PyValue::List(
            cur.rows
                .iter()
                .map(|r| {
                    PyValue::List(
                        cols.iter()
                            .map(|c| r.get(c).map_or(PyValue::None, super::db_to_py))
                            .collect(),
                    )
                })
                .collect(),
        ));
    }
    let cashflows = tables.pop().unwrap();
    let snapshots = tables.pop().unwrap();
    PyValue::Map(dict([
        ("runs", PyValue::List(runs)),
        ("snapshots", snapshots),
        ("cashflows", cashflows),
    ]))
}

struct SnapKite(Vec<PyDict>);

#[async_trait::async_trait]
impl sn::SnapshotKite for SnapKite {
    async fn is_authed(&self) -> bool {
        true
    }
    async fn holdings(&self) -> Result<Vec<PyDict>, String> {
        Ok(self.0.clone())
    }
    async fn available_cash(&self) -> Result<f64, String> {
        Ok(123_456.789)
    }
    async fn quote_raw(&self, _keys: &[String]) -> Result<PyDict, String> {
        Err("not used".into())
    }
    async fn instruments(&self, _exchange: &str) -> Result<Vec<PyValue>, String> {
        Err("not used".into())
    }
    async fn historical(&self, _token: i64, _day: NaiveDate) -> Result<Vec<PyValue>, String> {
        Err("not used".into())
    }
}

async fn snapshot_case(input: &PyDict, dir: &std::path::Path) -> PyValue {
    let kite = SnapKite(dicts(g(input, "holdings")));
    let opts = sn::CollectOptions {
        on_date: None,
        price_source: "holdings",
        cash: None,
        allow_stale_cash: false,
        today: date(&pys(input.get("today"))),
        include_untouchable_in_nav: g(input, "include").truthy(),
        root: dir,
    };
    let s = sn::collect(&kite, &opts).await.unwrap();
    PyValue::Map(dict([
        ("date", PyValue::Str(s.date)),
        ("nav", PyValue::Float(s.nav)),
        ("invested", PyValue::Float(s.invested)),
        ("cash", PyValue::Float(s.cash)),
        ("holdings_json", PyValue::Str(s.holdings_json)),
    ]))
}

#[tokio::test]
async fn every_golden_case_is_reproduced_byte_for_byte() {
    let root = PyValue::from_json(GOLDENS.as_bytes()).unwrap();
    let cases = l(g(m(&root), "cases"));
    assert_eq!(cases.len(), 41, "the golden set changed size");
    let dir = tempfile::tempdir().unwrap();
    let mut failures = Vec::new();
    for (n, case) in cases.iter().enumerate() {
        let case = m(case);
        let kind = pys(case.get("kind"));
        let input = m(g(case, "input"));
        let sub = dir.path().join(format!("c{n}"));
        std::fs::create_dir_all(&sub).unwrap();
        let got = match kind.as_str() {
            "holdings" => holdings_case(input),
            "tax" => tax_case(input),
            "parse" => parse_case(input, &sub, n),
            "fifo" => fifo_case(input),
            "tradebook_db" => tradebook_db_case(input, &sub).await,
            "positions" => positions_case(input),
            "reconcile" => reconcile_case(input).await,
            "snapshot" => snapshot_case(input, &sub).await,
            "snapshot_source" => snapshot_source_case(input, &sub).await,
            "parse_fallback" => parse_fallback_case(input, &sub, n),
            "reconcile_raise" => reconcile_raise_case(input).await,
            "snapshot_cli" => snapshot_cli_case(input, &sub).await,
            other => panic!("unknown kind {other}"),
        };
        let want = text(g(case, "output"));
        let got = text(&got);
        if want != got {
            failures.push(format!("case {n} ({kind}):\n  want {want}\n  got  {got}"));
        }
    }
    assert!(
        failures.is_empty(),
        "{} of {} DIFF:\n{}",
        failures.len(),
        cases.len(),
        failures.join("\n")
    );
}
