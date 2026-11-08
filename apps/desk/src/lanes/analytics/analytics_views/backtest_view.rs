//! `app/analytics/backtest_view.py` — chart geometry for the regime backtest page.
//!
//! Pure: the stored results dict in, pixel-space geometry out. Python's numbers keep their
//! type (an `int` tick prints `10`, a `float` one `10.0`), so arithmetic here runs on
//! [`Num`], which follows Python's int/float promotion, and the output is a [`PyValue`]
//! carrying exactly the types the Python dict carries.

use crate::lanes::exec_core::gtt::py_round;
use crate::pyfmt::{PyArg, format_value};
use crate::wire::{PyDict, PyValue, dict};

// ---------------------------------------------------------------------------------------------
// Python numbers
// ---------------------------------------------------------------------------------------------

/// A Python `int` or `float`, with Python's promotion rules.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Num {
    /// `int`.
    I(i64),
    /// `float`.
    F(f64),
}

impl Num {
    /// `float(self)`.
    #[must_use]
    #[allow(clippy::cast_precision_loss)]
    pub fn f(self) -> f64 {
        match self {
            Self::I(i) => i as f64,
            Self::F(x) => x,
        }
    }

    fn mul(self, o: Self) -> Self {
        match (self, o) {
            (Self::I(a), Self::I(b)) => Self::I(a.saturating_mul(b)),
            _ => Self::F(self.f() * o.f()),
        }
    }

    fn add(self, o: Self) -> Self {
        match (self, o) {
            (Self::I(a), Self::I(b)) => Self::I(a.saturating_add(b)),
            _ => Self::F(self.f() + o.f()),
        }
    }

    fn sub(self, o: Self) -> Self {
        match (self, o) {
            (Self::I(a), Self::I(b)) => Self::I(a.saturating_sub(b)),
            _ => Self::F(self.f() - o.f()),
        }
    }

    /// `round(self, n)`: an `int` stays an `int`.
    fn round(self, n: usize) -> Self {
        match self {
            Self::I(_) => self,
            Self::F(x) => Self::F(py_round(x, n)),
        }
    }

    fn truthy(self) -> bool {
        self.f() != 0.0
    }

    /// Python's `%` (the result takes the divisor's sign); only its truthiness is read.
    fn rem_nonzero(self, o: Self) -> bool {
        match (self, o) {
            (Self::I(a), Self::I(b)) if b != 0 => a.rem_euclid(b) != 0,
            _ => {
                let (a, b) = (self.f(), o.f());
                if b == 0.0 {
                    return false;
                }
                let r = a % b;
                r != 0.0
            }
        }
    }

    /// The value as Python prints it into the dict.
    #[must_use]
    pub fn py(self) -> PyValue {
        match self {
            Self::I(i) => PyValue::Int(i),
            Self::F(x) => PyValue::Float(x),
        }
    }
}

/// `int(x)` for a float: truncation toward zero.
#[allow(clippy::cast_possible_truncation)]
fn trunc(x: f64) -> i64 {
    x.trunc() as i64
}

/// What the stored results' JSON looks like once Python's `json.load` has read it: a
/// [`PyValue`] (order-preserving, ints kept as ints — `PyValue::from_json`).
type Json = PyValue;

/// The `dict`/`list`/`str` accessors the port reads the results with.
trait JsonLike {
    fn jget(&self, key: &str) -> Option<&PyValue>;
    fn jstr(&self) -> Option<&str>;
    fn jarr(&self) -> Option<&Vec<PyValue>>;
}

impl JsonLike for PyValue {
    fn jget(&self, key: &str) -> Option<&PyValue> {
        match self {
            PyValue::Map(m) => m.get(key),
            _ => None,
        }
    }
    fn jstr(&self) -> Option<&str> {
        match self {
            PyValue::Str(s) => Some(s),
            _ => None,
        }
    }
    fn jarr(&self) -> Option<&Vec<PyValue>> {
        match self {
            PyValue::List(xs) => Some(xs),
            _ => None,
        }
    }
}

impl JsonLike for PyDict {
    fn jget(&self, key: &str) -> Option<&PyValue> {
        self.get(key)
    }
    fn jstr(&self) -> Option<&str> {
        None
    }
    fn jarr(&self) -> Option<&Vec<PyValue>> {
        None
    }
}

fn num(v: &Json) -> Option<Num> {
    match v {
        PyValue::Int(i) => Some(Num::I(*i)),
        PyValue::Float(x) => Some(Num::F(*x)),
        PyValue::Bool(b) => Some(Num::I(i64::from(*b))),
        _ => None,
    }
}

/// `float(v)`; a non-number is NaN (Python would raise; stored results are numbers).
fn fl(v: Option<&Json>) -> f64 {
    v.and_then(num).map_or(f64::NAN, Num::f)
}

fn truthy(v: Option<&Json>) -> bool {
    v.is_some_and(PyValue::truthy)
}

/// A stored value as the page receives it (already Python-shaped).
fn json_py(v: &Json) -> PyValue {
    v.clone()
}

fn s(x: &str) -> PyValue {
    PyValue::Str(x.to_owned())
}

fn fv(x: f64) -> PyValue {
    PyValue::Float(x)
}

fn r1(x: f64) -> PyValue {
    PyValue::Float(py_round(x, 1))
}

fn ilen(n: usize) -> i64 {
    i64::try_from(n).unwrap_or(i64::MAX)
}

fn obj(v: &Json) -> Option<&PyDict> {
    match v {
        PyValue::Map(m) => Some(m),
        _ => None,
    }
}

fn arr(v: Option<&Json>) -> &[Json] {
    v.and_then(JsonLike::jarr).map_or(&[], Vec::as_slice)
}

// ---------------------------------------------------------------------------------------------
// families
// ---------------------------------------------------------------------------------------------

/// `FAMILIES`: key, label, colour, in Python's order.
pub const FAMILIES: [(&str, &str, &str); 3] = [
    ("none", "No overlay", "#2f9fb8"),
    ("binary", "Binary rule", "#c97d1e"),
    ("tiered", "Tiered", "#7b6ad0"),
];

/// `FAMILY_OF`.
pub const FAMILY_OF: [(&str, &str); 7] = [
    ("A", "none"),
    ("B_raw", "binary"),
    ("B_buf", "binary"),
    ("C_raw", "binary"),
    ("C_buf", "binary"),
    ("E", "tiered"),
    ("F", "tiered"),
];

fn family_of(key: &str) -> Option<&'static str> {
    FAMILY_OF.iter().find(|(k, _)| *k == key).map(|(_, f)| *f)
}

fn color(fam: &str) -> &'static str {
    FAMILIES
        .iter()
        .find(|(k, _, _)| *k == fam)
        .map_or("#5b6b8c", |(_, _, c)| *c)
}

/// `is_candidate(key)`.
#[must_use]
pub fn is_candidate(key: &str) -> bool {
    family_of(key).is_some()
}

fn families_py() -> PyValue {
    PyValue::List(
        FAMILIES
            .iter()
            .map(|(k, l, c)| PyValue::Map(dict([("key", s(k)), ("label", s(l)), ("color", s(c))])))
            .collect(),
    )
}

// ---------------------------------------------------------------------------------------------
// axes
// ---------------------------------------------------------------------------------------------

/// `_nice_step(span, target)`: 1, 2, 2.5 or 5 times a power of ten.
#[must_use]
pub fn nice_step(span: Num, target: i64) -> Num {
    if span.f() <= 0.0 {
        return Num::F(1.0);
    }
    // raw = span / max(target, 1): true division, a float.
    #[allow(clippy::cast_precision_loss)]
    let raw = span.f() / target.max(1) as f64;
    let mut mag = if raw.abs() >= 1.0 {
        let digits = trunc(raw.abs()).unsigned_abs().to_string().len();
        let exp = u32::try_from(digits.saturating_sub(1)).unwrap_or(0);
        Num::I(10_i64.saturating_pow(exp))
    } else {
        Num::F(0.1)
    };
    while mag.mul(Num::I(10)).f() <= raw {
        mag = mag.mul(Num::I(10));
    }
    for m in [Num::I(1), Num::I(2), Num::F(2.5), Num::I(5), Num::I(10)] {
        if mag.mul(m).f() >= raw {
            return mag.mul(m);
        }
    }
    mag.mul(Num::I(10))
}

/// `axis(lo, hi, target)`.
#[derive(Debug, Clone, PartialEq)]
pub struct Axis {
    /// `min`.
    pub min: Num,
    /// `max`.
    pub max: Num,
    /// `step`.
    pub step: Num,
    /// `ticks`.
    pub ticks: Vec<Num>,
}

impl Axis {
    fn py(&self) -> PyValue {
        PyValue::Map(dict([
            ("min", self.min.py()),
            ("max", self.max.py()),
            ("step", self.step.py()),
            (
                "ticks",
                PyValue::List(self.ticks.iter().map(|t| t.py()).collect()),
            ),
        ]))
    }
}

/// `axis(lo, hi, target=5)`: a padded, round-numbered axis covering `[lo, hi]`.
#[must_use]
pub fn axis(lo: Num, hi: Num, target: i64) -> Axis {
    let hi = if hi.f() <= lo.f() {
        lo.add(Num::I(1))
    } else {
        hi
    };
    let step = nice_step(hi.sub(lo), target);
    let start = if lo.truthy() {
        let q = trunc(lo.f() / step.f()) - i64::from(lo.rem_nonzero(step));
        step.mul(Num::I(q))
    } else {
        Num::F(0.0)
    };
    let q = trunc(hi.f() / step.f()) + i64::from(hi.rem_nonzero(step));
    let end = step.mul(Num::I(q));
    let mut ticks = Vec::new();
    let mut t = start;
    let limit = end.f() + step.f() * 1e-9;
    while t.f() <= limit && ticks.len() < 10_000 {
        ticks.push(t.round(6));
        t = t.add(step);
    }
    Axis {
        min: start,
        max: end,
        step,
        ticks,
    }
}

// ---------------------------------------------------------------------------------------------
// the scatter and its labels
// ---------------------------------------------------------------------------------------------

const LABEL_W: f64 = 104.0;
const LABEL_ABOVE: f64 = 11.0;
const LABEL_BELOW: f64 = 17.0;
const NUDGE: f64 = 27.0;
const MARK_R: f64 = 9.0;

#[derive(Debug, Clone)]
struct Mark {
    key: String,
    label: PyValue,
    family: &'static str,
    cagr: f64,
    dd: f64,
    x: f64,
    y: f64,
    best: bool,
    proxy: bool,
    // Set by `_place_labels`; `None` until assigned (Python's missing key).
    anchor: Option<&'static str>,
    lx: Option<f64>,
    ly: Option<f64>,
    leader: bool,
}

type Rect = (f64, f64, f64, f64);

fn label_box(m: &Mark) -> Rect {
    let lx = m.lx.unwrap_or(0.0);
    let ly = m.ly.unwrap_or(0.0);
    let x0 = if m.anchor == Some("start") {
        lx
    } else {
        lx - LABEL_W
    };
    (x0, ly - LABEL_ABOVE, x0 + LABEL_W, ly + LABEL_BELOW)
}

fn mark_box(m: &Mark) -> Rect {
    (m.x - MARK_R, m.y - MARK_R, m.x + MARK_R, m.y + MARK_R)
}

fn overlap(a: Rect, b: Rect) -> bool {
    a.0 < b.2 && b.0 < a.2 && a.1 < b.3 && b.1 < a.3
}

fn set_side(m: &mut Mark, anchor: &'static str) {
    m.anchor = Some(anchor);
    m.lx = Some(if anchor == "start" {
        m.x + 11.0
    } else {
        m.x - 11.0
    });
}

/// `_place_labels(marks, top=..., bottom=..., left=..., right=...)`.
fn place_labels(marks: &mut [Mark], top: f64, bottom: f64, left: f64, right: f64) {
    for i in 0..marks.len() {
        let home = marks[i].y + 4.0;
        let sides: [&'static str; 2] = if marks[i].x < 560.0 {
            ["start", "end"]
        } else {
            ["end", "start"]
        };
        let mut best: Option<(f64, &'static str)> = None;
        for attempt in 0_i32..14 {
            let sign = if attempt % 2 == 0 { 1.0 } else { -1.0 };
            let delta = f64::from((attempt + 1) / 2) * NUDGE * sign;
            marks[i].ly = Some(home + delta);
            let ly = home + delta;
            if ly - LABEL_ABOVE < top || ly + LABEL_BELOW > bottom {
                continue;
            }
            for anchor in sides {
                set_side(&mut marks[i], anchor);
                let (x0, _, x1, _) = label_box(&marks[i]);
                if x0 < left || x1 > right {
                    continue;
                }
                let me = label_box(&marks[i]);
                // `placed` is every earlier mark; `marks` (for dots) is every mark but this.
                let hits_label = marks[..i].iter().any(|p| overlap(me, label_box(p)));
                let hits_mark = marks
                    .iter()
                    .enumerate()
                    .any(|(j, o)| j != i && overlap(me, mark_box(o)));
                if !hits_label && !hits_mark {
                    best = Some((ly, anchor));
                    break;
                }
            }
            if best.is_some() {
                break;
            }
        }
        if let Some((ly, anchor)) = best {
            marks[i].ly = Some(ly);
            set_side(&mut marks[i], anchor);
        }
        marks[i].leader = (marks[i].ly.unwrap_or(home) - home).abs() > 1.0;
    }
}

fn ok_status(v: &Json) -> bool {
    v.jget("status").and_then(JsonLike::jstr) == Some("ok")
}

fn label_of(k: &str, v: &Json) -> PyValue {
    v.jget("label").map_or_else(|| s(k), json_py)
}

/// `risk_return_scatter(variants)`: CAGR against the drawdown sat through.
#[must_use]
#[allow(clippy::too_many_lines)]
pub fn risk_return_scatter(variants: &PyDict) -> PyValue {
    let (width, height, pad_l, pad_b, pad_t, pad_r) =
        (720_i64, 380_i64, 62_i64, 46_i64, 18_i64, 22_i64);
    let pts: Vec<(&str, &Json)> = variants
        .iter()
        .filter(|(k, v)| is_candidate(k) && ok_status(v))
        .collect();
    if pts.is_empty() {
        return PyValue::None;
    }
    let xs: Vec<f64> = pts
        .iter()
        .map(|(_, v)| fl(v.jget("max_drawdown_pct")).abs())
        .collect();
    let ys: Vec<f64> = pts.iter().map(|(_, v)| fl(v.jget("CAGR"))).collect();
    let max_x = xs.iter().copied().fold(f64::NEG_INFINITY, f64::max);
    let max_y = ys.iter().copied().fold(f64::NEG_INFINITY, f64::max);
    let min_y = ys.iter().copied().fold(f64::INFINITY, f64::min);
    let ax = axis(Num::I(0), Num::F(max_x), 5);
    // min(0, min(ys)): the int 0 unless a y is strictly below it.
    let ylo = if min_y < 0.0 {
        Num::F(min_y)
    } else {
        Num::I(0)
    };
    let ay = axis(ylo, Num::F(max_y), 5);
    #[allow(clippy::cast_precision_loss)]
    let (pw, ph) = (
        (width - pad_l - pad_r) as f64,
        (height - pad_t - pad_b) as f64,
    );
    #[allow(clippy::cast_precision_loss)]
    let (pl, pt) = (pad_l as f64, pad_t as f64);
    let sx = |x: f64| pl + (x - ax.min.f()) / (ax.max.f() - ax.min.f()) * pw;
    let sy = |y: f64| pt + (1.0 - (y - ay.min.f()) / (ay.max.f() - ay.min.f())) * ph;

    // max(pts, key=...): the first maximal element.
    let mut best_key: Option<&str> = None;
    let mut best_score = f64::NEG_INFINITY;
    for (k, v) in &pts {
        let score = fl(v.jget("CAGR")) / fl(v.jget("max_drawdown_pct")).abs().max(1e-9);
        if best_key.is_none() || score > best_score {
            best_key = Some(k);
            best_score = score;
        }
    }

    let mut marks: Vec<Mark> = pts
        .iter()
        .map(|(k, v)| {
            let fam = family_of(k).unwrap_or("tiered");
            let dd = fl(v.jget("max_drawdown_pct")).abs();
            let cagr = fl(v.jget("CAGR"));
            Mark {
                key: (*k).to_owned(),
                label: label_of(k, v),
                family: fam,
                cagr: py_round(cagr, 2),
                dd: py_round(dd, 2),
                x: py_round(sx(dd), 1),
                y: py_round(sy(cagr), 1),
                best: best_key == Some(k),
                proxy: truthy(v.jget("note")) || fam == "tiered",
                anchor: None,
                lx: None,
                ly: None,
                leader: false,
            }
        })
        .collect();
    // list.sort(key=x) is stable.
    marks.sort_by(|a, b| a.x.partial_cmp(&b.x).unwrap_or(std::cmp::Ordering::Equal));
    #[allow(clippy::cast_precision_loss)]
    place_labels(&mut marks, pt, pt + ph, 4.0, width as f64 - 4.0);

    let mark_py = |m: &Mark| {
        let mut d = dict([
            ("key", s(&m.key)),
            ("label", m.label.clone()),
            ("family", s(m.family)),
            ("color", s(color(m.family))),
            ("cagr", fv(m.cagr)),
            ("dd", fv(m.dd)),
            ("x", fv(m.x)),
            ("y", fv(m.y)),
            ("best", PyValue::Bool(m.best)),
            ("proxy", PyValue::Bool(m.proxy)),
        ]);
        // Python sets `ly` first (each attempt), then `anchor` and `lx` together.
        if let Some(ly) = m.ly {
            d.insert("ly", fv(ly));
        }
        if let Some(a) = m.anchor {
            d.insert("anchor", s(a));
        }
        if let Some(lx) = m.lx {
            d.insert("lx", fv(lx));
        }
        d.insert("leader", PyValue::Bool(m.leader));
        PyValue::Map(d)
    };

    let zero_y = if ay.min.f() < 0.0 && 0.0 < ay.max.f() {
        r1(sy(0.0))
    } else {
        PyValue::None
    };
    PyValue::Map(dict([
        ("width", PyValue::Int(width)),
        ("height", PyValue::Int(height)),
        ("pad_l", PyValue::Int(pad_l)),
        ("pad_t", PyValue::Int(pad_t)),
        ("pad_b", PyValue::Int(pad_b)),
        ("pad_r", PyValue::Int(pad_r)),
        ("plot_w", PyValue::Int(width - pad_l - pad_r)),
        ("plot_h", PyValue::Int(height - pad_t - pad_b)),
        ("x", ax.py()),
        ("y", ay.py()),
        (
            "xticks",
            PyValue::List(
                ax.ticks
                    .iter()
                    .map(|t| PyValue::Map(dict([("v", t.py()), ("px", r1(sx(t.f())))])))
                    .collect(),
            ),
        ),
        (
            "yticks",
            PyValue::List(
                ay.ticks
                    .iter()
                    .map(|t| PyValue::Map(dict([("v", t.py()), ("px", r1(sy(t.f())))])))
                    .collect(),
            ),
        ),
        ("zero_y", zero_y),
        ("marks", PyValue::List(marks.iter().map(mark_py).collect())),
        ("families", families_py()),
    ]))
}

/// `ranked_bars(variants, metric, keys=..., pad_l=...)`: one measure, sorted.
#[must_use]
pub fn ranked_bars(
    variants: &PyDict,
    metric: &str,
    keys: Option<&[String]>,
    pad_l: i64,
) -> PyValue {
    let (width, row_h, pad_r) = (720_i64, 26_i64, 62_i64);
    let mut rows: Vec<(&str, &Json)> = variants
        .iter()
        .filter(|(k, v)| {
            ok_status(v)
                && v.jget(metric).is_some()
                && keys.map_or_else(|| is_candidate(k), |ks| ks.iter().any(|x| x == k))
        })
        .collect();
    if rows.is_empty() {
        return PyValue::None;
    }
    // sort(key=..., reverse=True) is stable and keeps equal keys in their original order.
    rows.sort_by(|a, b| {
        fl(b.1.jget(metric))
            .partial_cmp(&fl(a.1.jget(metric)))
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    let vals: Vec<f64> = rows.iter().map(|(_, v)| fl(v.jget(metric))).collect();
    let vmax = vals.iter().copied().fold(f64::NEG_INFINITY, f64::max);
    let vmin = vals.iter().copied().fold(f64::INFINITY, f64::min);
    let hi = {
        let h = vmax.max(0.0);
        if h == 0.0 { 1.0 } else { h }
    };
    let lo = vmin.min(0.0);
    let span = {
        let sp = hi - lo;
        if sp == 0.0 { 1.0 } else { sp }
    };
    #[allow(clippy::cast_precision_loss)]
    let (bw, pl) = ((width - pad_l - pad_r) as f64, pad_l as f64);
    let zero = pl + (0.0 - lo) / span * bw;
    let mut bars = Vec::new();
    for (i, (k, v)) in rows.iter().enumerate() {
        let val = fl(v.jget(metric));
        let x = pl + (val.min(0.0) - lo) / span * bw;
        let w = val.abs() / span * bw;
        let fam = family_of(k).unwrap_or("tiered");
        bars.push(PyValue::Map(dict([
            ("key", s(k)),
            ("label", label_of(k, v)),
            ("value", fv(py_round(val, 3))),
            ("y", PyValue::Int(ilen(i) * row_h)),
            ("x", r1(x)),
            ("w", r1(w.max(1.5))),
            (
                "color",
                s(if is_candidate(k) {
                    color(fam)
                } else {
                    "#5b6b8c"
                }),
            ),
            ("family", s(fam)),
        ])));
    }
    let n = ilen(bars.len());
    PyValue::Map(dict([
        ("width", PyValue::Int(width)),
        ("height", PyValue::Int(n * row_h + 8)),
        ("row_h", PyValue::Int(row_h)),
        ("pad_l", PyValue::Int(pad_l)),
        ("zero", r1(zero)),
        ("bars", PyValue::List(bars)),
        ("metric", s(metric)),
    ]))
}

// ---------------------------------------------------------------------------------------------
// curves
// ---------------------------------------------------------------------------------------------

/// `CURVE_CALLOUTS`.
pub const CURVE_CALLOUTS: [&str; 3] = ["A", "B_raw", "F"];
/// `EXPOSURE_WINDOW`.
pub const EXPOSURE_WINDOW: usize = 52;

/// `_log_ticks(lo, hi)`: 1-2-5 decade ticks spanning `[lo, hi]`.
///
/// `lo`/`hi` keep the types Python's `min`/`max` returned, because the empty-range fallback
/// `[lo, hi]` prints them as they are (`158`, not `158.0`).
fn log_ticks(lo_n: Num, hi_n: Num) -> Vec<Num> {
    let (lo, hi) = (lo_n.f(), hi_n.f());
    let e = lo.max(1e-9).log10().floor();
    // 10 ** int: an int for a non-negative exponent, a float otherwise.
    #[allow(clippy::cast_possible_truncation)]
    let e = e as i32;
    let mut dec = match u32::try_from(e) {
        Ok(p) => Num::I(10_i64.saturating_pow(p)),
        Err(_) => Num::F(10_f64.powi(e)),
    };
    let mut out = Vec::new();
    while dec.f() <= hi * 10.0 && out.len() < 10_000 {
        for m in [1, 2, 5] {
            let v = dec.mul(Num::I(m));
            if lo <= v.f() && v.f() <= hi {
                out.push(v);
            }
        }
        dec = dec.mul(Num::I(10));
        if dec.f().is_infinite() {
            break;
        }
    }
    if out.is_empty() {
        vec![lo_n, hi_n]
    } else {
        out
    }
}

fn dd_ticks(worst: f64) -> Vec<f64> {
    let step = if worst > -45.0 { 10_i32 } else { 20 };
    let mut out = vec![0.0];
    let mut v = -step;
    while f64::from(v) > worst - f64::from(step) && out.len() < 10_000 {
        out.push(f64::from(v));
        v -= step;
    }
    out
}

fn fmt(v: f64, spec: &str) -> String {
    format_value(&PyArg::Float(v), spec).unwrap_or_default()
}

fn fmt_num(v: Num, spec: &str) -> String {
    let arg = match v {
        Num::I(i) => PyArg::Int(i),
        Num::F(x) => PyArg::Float(x),
    };
    format_value(&arg, spec).unwrap_or_default()
}

/// A series value: `None`, or a number.
fn opt_num(v: &Json) -> Option<Num> {
    num(v)
}

struct Line {
    key: String,
    family: &'static str,
    equity: String,
    drawdown: String,
    exposure: String,
    avg_exposure: PyValue,
    callout: bool,
    final_: PyValue,
    // `int` once `_spread` clamps it to an int bound, as Python's max()/min() return it.
    label_y: Option<Num>,
}

/// `curves(data)`: equity (log), drawdown and exposure over the whole window.
#[must_use]
#[allow(clippy::too_many_lines)]
pub fn curves(data: &Json) -> PyValue {
    let (width, eq_h, dd_h, ex_h, pad_l, pad_r, pad_t, pad_b) = (
        720_i64, 300_i64, 190_i64, 150_i64, 58_i64, 74_i64, 14_i64, 26_i64,
    );
    let c = data.jget("curves").filter(|x| truthy(Some(x)));
    let dates: Vec<String> = arr(c.and_then(|c| c.jget("dates")))
        .iter()
        .map(|d| d.jstr().unwrap_or_default().to_owned())
        .collect();
    let empty = PyDict::new();
    let series = c
        .and_then(|c| c.jget("series"))
        .and_then(obj)
        .unwrap_or(&empty);
    if dates.len() < 2 || series.is_empty() {
        return PyValue::None;
    }
    let keys: Vec<String> = series
        .iter()
        .map(|(k, _)| k.to_owned())
        .filter(|k| is_candidate(k))
        .collect();
    if keys.is_empty() {
        return PyValue::None;
    }
    let field = |k: &str, f: &str| -> Vec<Option<Num>> {
        arr(series.jget(k).and_then(|x| x.jget(f)))
            .iter()
            .map(opt_num)
            .collect()
    };
    let eq_vals: Vec<Num> = keys
        .iter()
        .flat_map(|k| field(k, "equity"))
        .filter_map(|v| v.filter(|n| n.truthy()))
        .collect();
    let dd_vals: Vec<Num> = keys
        .iter()
        .flat_map(|k| field(k, "drawdown"))
        .flatten()
        .collect();
    if eq_vals.is_empty() || dd_vals.is_empty() {
        return PyValue::None;
    }
    // `lo, hi = max(min(eq_vals), 1e-6), max(eq_vals)`: min/max return the first extreme
    // element with its type (an int stays an int), and `max(x, 1e-6)` is `x` unless 1e-6
    // is strictly greater.
    let mut lo_n = eq_vals[0];
    let mut hi_n = eq_vals[0];
    for v in &eq_vals[1..] {
        if v.f() < lo_n.f() {
            lo_n = *v;
        }
        if v.f() > hi_n.f() {
            hi_n = *v;
        }
    }
    if 1e-6 > lo_n.f() {
        lo_n = Num::F(1e-6);
    }
    if hi_n.f() <= lo_n.f() {
        (lo_n, hi_n) = (Num::F(lo_n.f() / 2.0), Num::F(hi_n.f() * 2.0));
    }
    let (lo, hi) = (lo_n.f(), hi_n.f());
    // min(dd_vals): the first minimal element, its type kept.
    let mut worst = dd_vals[0];
    for v in &dd_vals[1..] {
        if v.f() < worst.f() {
            worst = *v;
        }
    }
    let worst_f = worst.f();

    #[allow(clippy::cast_precision_loss)]
    let (pw, eph, dph, xph, pl, pt) = (
        (width - pad_l - pad_r) as f64,
        (eq_h - pad_t - pad_b) as f64,
        (dd_h - pad_t - pad_b) as f64,
        (ex_h - pad_t - pad_b) as f64,
        pad_l as f64,
        pad_t as f64,
    );
    #[allow(clippy::cast_precision_loss)]
    let n = (dates.len() - 1) as f64;
    let (llo, lhi) = (lo.log10(), hi.log10());
    #[allow(clippy::cast_precision_loss)]
    let px = |i: usize| pl + i as f64 / n * pw;
    let eqy = |v: f64| pt + (1.0 - (v.log10() - llo) / (lhi - llo)) * eph;
    let ddy = |v: f64| {
        if worst_f == 0.0 {
            pt
        } else {
            pt + (-v / -worst_f) * dph
        }
    };
    let exy = |v: f64| pt + (1.0 - v / 100.0) * xph;

    let smooth = |vals: &[Option<Num>]| -> Vec<Option<f64>> {
        let mut out = Vec::new();
        let mut acc: Vec<f64> = Vec::new();
        for v in vals {
            match v {
                None => out.push(None),
                Some(x) => {
                    acc.push(x.f());
                    if acc.len() > EXPOSURE_WINDOW {
                        acc.remove(0);
                    }
                    let mut sum = 0.0;
                    for a in &acc {
                        sum += a;
                    }
                    #[allow(clippy::cast_precision_loss)]
                    out.push(Some(sum / acc.len() as f64));
                }
            }
        }
        out
    };
    let path = |vals: &[Option<f64>], which: u8| -> String {
        let mut out = Vec::new();
        let mut pen = "M";
        for (i, v) in vals.iter().enumerate() {
            let Some(v) = v else {
                pen = "M";
                continue;
            };
            if which == 0 && *v <= 0.0 {
                pen = "M";
                continue;
            }
            let y = match which {
                0 => eqy(*v),
                1 => ddy(*v),
                _ => exy(*v),
            };
            out.push(format!("{pen}{:.1},{:.1}", px(i), y));
            pen = "L";
        }
        out.join(" ")
    };
    let as_f =
        |xs: &[Option<Num>]| -> Vec<Option<f64>> { xs.iter().map(|x| x.map(Num::f)).collect() };

    let mut ordered: Vec<&String> = keys.iter().collect();
    ordered.sort_by_key(|k| !CURVE_CALLOUTS.contains(&k.as_str()));
    let mut lines: Vec<Line> = Vec::new();
    for k in ordered {
        let fam = family_of(k).unwrap_or("tiered");
        let eq = field(k, "equity");
        let dd = field(k, "drawdown");
        let ex_raw = series.jget(k).and_then(|x| x.jget("exposure"));
        let ex = field(k, "exposure");
        let ex_truthy = truthy(ex_raw);
        let last = eq.iter().rev().find_map(|v| v.filter(|n| n.truthy()));
        let present: Vec<f64> = ex.iter().flatten().map(|n| n.f()).collect();
        let avg = if ex_truthy {
            let mut sum = 0.0;
            for x in &present {
                sum += x;
            }
            #[allow(clippy::cast_precision_loss)]
            let denom = present.len().max(1) as f64;
            r1(sum / denom)
        } else {
            PyValue::None
        };
        lines.push(Line {
            key: k.clone(),
            family: fam,
            equity: path(&as_f(&eq), 0),
            drawdown: path(&as_f(&dd), 1),
            exposure: if ex_truthy {
                path(&smooth(&ex), 2)
            } else {
                String::new()
            },
            avg_exposure: avg,
            callout: CURVE_CALLOUTS.contains(&k.as_str()),
            final_: last.map_or(PyValue::None, |l| l.round(0).py()),
            label_y: last.map(|l| Num::F(py_round(eqy(l.f()), 1))),
        });
    }
    // _spread over the callouts that have a label_y, sorted by it (stable).
    {
        let mut idx: Vec<usize> = (0..lines.len())
            .filter(|&i| lines[i].callout && lines[i].label_y.is_some())
            .collect();
        idx.sort_by(|&a, &b| {
            let ya = lines[a].label_y.map_or(0.0, Num::f);
            let yb = lines[b].label_y.map_or(0.0, Num::f);
            ya.partial_cmp(&yb).unwrap_or(std::cmp::Ordering::Equal)
        });
        let gap = 13.0;
        for w in 1..idx.len() {
            let (prev, cur) = (idx[w - 1], idx[w]);
            let (py_, cy) = (
                lines[prev].label_y.map_or(0.0, Num::f),
                lines[cur].label_y.map_or(0.0, Num::f),
            );
            if cy - py_ < gap {
                lines[cur].label_y = Some(Num::F(py_ + gap));
            }
        }
        // min(max(y, top + 6), bottom), both bounds ints; max/min return the first
        // extreme argument, so a tie keeps `y`.
        let (top6, bottom) = (Num::I(pad_t + 6), Num::I(pad_t + (eq_h - pad_t - pad_b)));
        for &i in &idx {
            let y = lines[i].label_y.unwrap_or(Num::F(0.0));
            let y = if top6.f() > y.f() { top6 } else { y };
            let y = if bottom.f() < y.f() { bottom } else { y };
            lines[i].label_y = Some(y);
        }
    }

    let year_ix: Vec<usize> = dates
        .iter()
        .enumerate()
        .filter(|(i, d)| d.ends_with("-01-01") || (*i > 0 && dates[i - 1].get(..4) != d.get(..4)))
        .map(|(i, _)| i)
        .collect();
    let step = (year_ix.len() / 8).max(1);

    let lines_py: Vec<PyValue> = lines
        .iter()
        .map(|l| {
            PyValue::Map(dict([
                ("key", s(&l.key)),
                ("family", s(l.family)),
                ("color", s(color(l.family))),
                ("equity", s(&l.equity)),
                ("drawdown", s(&l.drawdown)),
                ("exposure", s(&l.exposure)),
                ("avg_exposure", l.avg_exposure.clone()),
                ("callout", PyValue::Bool(l.callout)),
                ("final", l.final_.clone()),
                ("label_y", l.label_y.map_or(PyValue::None, Num::py)),
            ]))
        })
        .collect();

    PyValue::Map(dict([
        ("width", PyValue::Int(width)),
        ("eq_h", PyValue::Int(eq_h)),
        ("dd_h", PyValue::Int(dd_h)),
        ("ex_h", PyValue::Int(ex_h)),
        (
            "has_exposure",
            PyValue::Bool(lines.iter().any(|l| !l.exposure.is_empty())),
        ),
        ("exposure_window", PyValue::Int(ilen(EXPOSURE_WINDOW))),
        (
            "ex_ticks",
            PyValue::List(
                [0_i64, 50, 100]
                    .iter()
                    .map(|v| {
                        #[allow(clippy::cast_precision_loss)]
                        let y = exy(*v as f64);
                        PyValue::Map(dict([("v", PyValue::Int(*v)), ("px", r1(y))]))
                    })
                    .collect(),
            ),
        ),
        ("pad_l", PyValue::Int(pad_l)),
        ("pad_r", PyValue::Int(pad_r)),
        ("pad_t", PyValue::Int(pad_t)),
        ("eq_plot", PyValue::Int(eq_h - pad_t - pad_b)),
        ("dd_plot", PyValue::Int(dd_h - pad_t - pad_b)),
        ("lines", PyValue::List(lines_py)),
        (
            "eq_ticks",
            PyValue::List(
                log_ticks(lo_n, hi_n)
                    .into_iter()
                    .map(|v| {
                        PyValue::Map(dict([
                            ("v", v.py()),
                            ("px", r1(eqy(v.f()))),
                            ("label", s(&fmt_num(v, ",.0f"))),
                        ]))
                    })
                    .collect(),
            ),
        ),
        (
            "dd_ticks",
            PyValue::List(
                dd_ticks(worst_f)
                    .into_iter()
                    .map(|v| {
                        PyValue::Map(dict([
                            ("v", fv(v)),
                            // ddy returns the int `pad_t` when nothing ever fell.
                            (
                                "px",
                                if worst_f == 0.0 {
                                    PyValue::Int(pad_t)
                                } else {
                                    r1(ddy(v))
                                },
                            ),
                            ("label", s(&format!("{}%", fmt(v, ".0f")))),
                        ]))
                    })
                    .collect(),
            ),
        ),
        (
            "xticks",
            PyValue::List(
                year_ix
                    .iter()
                    .step_by(step)
                    .map(|&i| {
                        PyValue::Map(dict([
                            ("px", r1(px(i))),
                            ("label", s(dates[i].get(..4).unwrap_or(&dates[i]))),
                        ]))
                    })
                    .collect(),
            ),
        ),
        ("start", s(&dates[0])),
        ("end", s(&dates[dates.len() - 1])),
        ("worst", worst.round(2).py()),
    ]))
}

// ---------------------------------------------------------------------------------------------
// robustness, timelines, subperiods
// ---------------------------------------------------------------------------------------------

/// `RAMP`.
pub const RAMP: [&str; 5] = ["#8577d0", "#9a8ce4", "#b0a4f2", "#c5bcf8", "#dad3fc"];
/// `RAMP_INK`.
pub const RAMP_INK: &str = "#0b1220";

fn ramp_color(v: f64, lo: f64, hi: f64) -> &'static str {
    if hi <= lo {
        return RAMP[RAMP.len() / 2];
    }
    #[allow(clippy::cast_precision_loss)]
    let i = trunc((v - lo) / (hi - lo) * (RAMP.len() - 1) as f64 + 0.5);
    let top = ilen(RAMP.len() - 1);
    RAMP[usize::try_from(i.clamp(0, top)).unwrap_or(0)]
}

/// Python's `==` between two numbers (`1 == 1.0`), exact as Python's is.
#[allow(clippy::float_cmp)]
fn num_eq(a: Num, b: Num) -> bool {
    a.f() == b.f()
}

/// Python's ordering for the `sorted({...})` of JSON scalars (numbers; strings sort apart).
fn sorted_unique(vals: Vec<&Json>) -> Vec<Json> {
    let mut out: Vec<Json> = Vec::new();
    for v in vals {
        // Set membership: 1 == 1.0 in Python.
        let dup = out.iter().any(|o| match (num(o), num(v)) {
            (Some(a), Some(b)) => num_eq(a, b),
            _ => o == v,
        });
        if !dup {
            out.push(v.clone());
        }
    }
    out.sort_by(|a, b| match (num(a), num(b)) {
        (Some(x), Some(y)) => x
            .f()
            .partial_cmp(&y.f())
            .unwrap_or(std::cmp::Ordering::Equal),
        _ => a
            .jstr()
            .unwrap_or_default()
            .cmp(b.jstr().unwrap_or_default()),
    });
    out
}

fn same(a: Option<&Json>, b: &Json) -> bool {
    match (a.and_then(num), num(b)) {
        (Some(x), Some(y)) => num_eq(x, y),
        _ => a == Some(b),
    }
}

/// `robustness(data, metric)`: the parameter grid as heatmaps, one per R4 setting.
#[must_use]
pub fn robustness(data: &Json, metric: &str) -> PyValue {
    let rows = arr(data.jget("robustness"));
    if rows.is_empty() {
        return PyValue::None;
    }
    let vals: Vec<f64> = rows.iter().map(|r| fl(r.jget(metric))).collect();
    let lo = vals.iter().copied().fold(f64::INFINITY, f64::min);
    let hi = vals.iter().copied().fold(f64::NEG_INFINITY, f64::max);
    let pick = |f: &str| sorted_unique(rows.iter().filter_map(|r| r.jget(f)).collect());
    let buffers = pick("buffer_bps");
    let confirms = pick("confirm_days");
    let r4s = pick("r4_exposure");

    let mut grids = Vec::new();
    let mut signatures: Vec<Vec<f64>> = Vec::new();
    for r4 in &r4s {
        let mut cells = Vec::new();
        let mut sig = Vec::new();
        for b in &buffers {
            for c in &confirms {
                let Some(hit) = rows.iter().find(|r| {
                    same(r.jget("buffer_bps"), b)
                        && same(r.jget("confirm_days"), c)
                        && same(r.jget("r4_exposure"), r4)
                }) else {
                    continue;
                };
                let value = py_round(fl(hit.jget(metric)), 2);
                sig.push(value);
                cells.push(PyValue::Map(dict([
                    ("buffer", json_py(b)),
                    ("confirm", json_py(c)),
                    ("value", fv(value)),
                    ("dd", fv(py_round(fl(hit.jget("max_drawdown_pct")), 1))),
                    ("whip", fv(py_round(fl(hit.jget("whipsaws_per_year")), 2))),
                    (
                        "turnover",
                        fv(py_round(fl(hit.jget("annual_turnover_pct")), 0)),
                    ),
                    ("color", s(ramp_color(fl(hit.jget(metric)), lo, hi))),
                ])));
            }
        }
        signatures.push(sig);
        grids.push(PyValue::Map(dict([
            ("r4", json_py(r4)),
            ("cells", PyValue::List(cells)),
        ])));
    }
    let r4_inert = signatures.len() > 1 && signatures.iter().all(|s| *s == signatures[0]);
    PyValue::Map(dict([
        ("grids", PyValue::List(grids)),
        (
            "buffers",
            PyValue::List(buffers.iter().map(json_py).collect()),
        ),
        (
            "confirms",
            PyValue::List(confirms.iter().map(json_py).collect()),
        ),
        ("metric", s(metric)),
        ("min", fv(py_round(lo, 2))),
        ("max", fv(py_round(hi, 2))),
        ("spread", fv(py_round(hi - lo, 2))),
        ("ramp", PyValue::List(RAMP.iter().map(|c| s(c)).collect())),
        ("ink", s(RAMP_INK)),
        ("r4_inert", PyValue::Bool(r4_inert)),
        ("narrow", PyValue::Bool((hi - lo) < 2.0)),
    ]))
}

/// `timelines(data)`: exposure through each event window, as a step.
#[must_use]
pub fn timelines(data: &Json) -> PyValue {
    let (width, height, pad_l, pad_r, pad_t, pad_b) =
        (720_i64, 118_i64, 40_i64, 16_i64, 12_i64, 24_i64);
    let wins = arr(data.jget("timelines"));
    if wins.is_empty() {
        return PyValue::None;
    }
    let mut out = Vec::new();
    for w in wins {
        let get = |k: &str| w.jget(k).map_or(PyValue::None, json_py);
        let mut entry = dict([
            ("label", get("label")),
            ("start", get("start")),
            ("end", get("end")),
            ("width", PyValue::Int(width)),
            ("height", PyValue::Int(height)),
            ("pad_l", PyValue::Int(pad_l)),
            ("pad_t", PyValue::Int(pad_t)),
            ("unavailable", get("unavailable")),
        ]);
        let rows = arr(w.jget("rows"));
        let unavailable = truthy(w.jget("unavailable"));
        if unavailable || rows.is_empty() {
            if !unavailable {
                entry.insert("unavailable", s("no evaluations in this window"));
            }
            out.push(PyValue::Map(entry));
            continue;
        }
        #[allow(clippy::cast_precision_loss)]
        let (n, pw, ph, pl, pt) = (
            (rows.len().saturating_sub(1)).max(1) as f64,
            (width - pad_l - pad_r) as f64,
            (height - pad_t - pad_b) as f64,
            pad_l as f64,
            pad_t as f64,
        );
        #[allow(clippy::cast_precision_loss)]
        let px = |i: usize| pl + i as f64 / n * pw;
        let py_ = |v: f64| pt + (1.0 - v / 100.0) * ph;
        let ex = |r: &Json| fl(r.jget("exposure"));
        let mut d = vec![format!("M{:.1},{:.1}", px(0), py_(ex(&rows[0])))];
        for i in 1..rows.len() {
            d.push(format!("L{:.1},{:.1}", px(i), py_(ex(&rows[i - 1]))));
            d.push(format!("L{:.1},{:.1}", px(i), py_(ex(&rows[i]))));
        }
        let path = d.join(" ");
        let area = format!(
            "{path} L{:.1},{:.1} L{:.1},{:.1} Z",
            px(rows.len() - 1),
            py_(0.0),
            px(0),
            py_(0.0)
        );
        let mut changes = Vec::new();
        for (i, r) in rows.iter().enumerate() {
            if i > 0 && r.jget("tier") != rows[i - 1].jget("tier") {
                changes.push(PyValue::Map(dict([
                    ("x", r1(px(i))),
                    ("tier", r.jget("tier").map_or(PyValue::None, json_py)),
                    ("date", r.jget("date").map_or(PyValue::None, json_py)),
                    (
                        "exposure",
                        r.jget("exposure").map_or(PyValue::None, json_py),
                    ),
                ])));
            }
        }
        let mut min_ex: Option<&Json> = None;
        for r in rows {
            if let Some(e) = r.jget("exposure")
                && min_ex.is_none_or(|m| fl(Some(e)) < fl(Some(m)))
            {
                min_ex = Some(e);
            }
        }
        entry.insert("path", s(&path));
        entry.insert("area", s(&area));
        entry.insert("changes", PyValue::List(changes));
        entry.insert(
            "ticks",
            PyValue::List(
                [0_i64, 50, 100]
                    .iter()
                    .map(|v| {
                        #[allow(clippy::cast_precision_loss)]
                        let y = py_(*v as f64);
                        PyValue::Map(dict([("v", PyValue::Int(*v)), ("px", r1(y))]))
                    })
                    .collect(),
            ),
        );
        entry.insert("first", rows[0].jget("date").map_or(PyValue::None, json_py));
        entry.insert(
            "last",
            rows[rows.len() - 1]
                .jget("date")
                .map_or(PyValue::None, json_py),
        );
        entry.insert("min_exposure", min_ex.map_or(PyValue::None, json_py));
        out.push(PyValue::Map(entry));
    }
    PyValue::List(out)
}

/// `subperiods(data)`: the overlay against no overlay in windows chosen because they hurt.
#[must_use]
pub fn subperiods(data: &Json) -> PyValue {
    let bar_w = 190_i64;
    let rows = arr(data.jget("subperiods"));
    if rows.is_empty() {
        return PyValue::None;
    }
    let usable: Vec<&Json> = rows
        .iter()
        .filter(|r| !truthy(r.jget("unavailable")) && truthy(r.jget("variants")))
        .collect();
    if usable.is_empty() {
        return PyValue::None;
    }
    let keys: Vec<String> = usable[0]
        .jget("variants")
        .and_then(obj)
        .map(|m| m.iter().map(|(k, _)| k.to_owned()).collect())
        .unwrap_or_default();
    let all = |f: &str| -> f64 {
        usable
            .iter()
            .flat_map(|r| {
                r.jget("variants")
                    .and_then(obj)
                    .into_iter()
                    .flat_map(|m| m.iter().map(|(_, v)| v))
            })
            .map(|v| fl(v.jget(f)).abs())
            .fold(f64::NEG_INFINITY, f64::max)
    };
    let cagr_max = all("CAGR");
    let dd_max = all("max_drawdown_pct");
    #[allow(clippy::cast_precision_loss)]
    let bw = bar_w as f64;
    let mut out = Vec::new();
    for r in &usable {
        let mut bars = Vec::new();
        for k in &keys {
            let v = r
                .jget("variants")
                .and_then(|m| m.jget(k))
                .unwrap_or(&PyValue::None);
            let fam = family_of(k).unwrap_or("tiered");
            let cagr = fl(v.jget("CAGR"));
            let dd = fl(v.jget("max_drawdown_pct"));
            bars.push(PyValue::Map(dict([
                ("key", s(k)),
                ("color", s(color(fam))),
                ("cagr", fv(py_round(cagr, 2))),
                ("dd", fv(py_round(dd, 2))),
                ("cagr_w", r1(cagr.abs() / cagr_max * bw)),
                ("dd_w", r1(dd.abs() / dd_max * bw)),
            ])));
        }
        out.push(PyValue::Map(dict([
            ("label", r.jget("label").map_or(PyValue::None, json_py)),
            ("start", r.jget("start").map_or(PyValue::None, json_py)),
            ("bars", PyValue::List(bars)),
        ])));
    }
    PyValue::Map(dict([
        ("rows", PyValue::List(out)),
        ("keys", PyValue::List(keys.iter().map(|k| s(k)).collect())),
        ("bar_w", PyValue::Int(bar_w)),
        ("cagr_max", r1(cagr_max)),
        ("dd_max", r1(dd_max)),
    ]))
}

/// `build(data)`: everything the backtest page needs to draw itself; `None` without
/// variants.
#[must_use]
pub fn build(data: Option<&Json>) -> PyValue {
    let Some(data) = data.filter(|d| truthy(Some(d))) else {
        return PyValue::None;
    };
    let Some(v) = data
        .jget("variants")
        .filter(|v| truthy(Some(v)))
        .and_then(obj)
    else {
        return PyValue::None;
    };
    let abl: Vec<String> = v
        .iter()
        .map(|(k, _)| k.to_owned())
        .filter(|k| k.starts_with("abl_"))
        .collect();
    let caveat = data
        .jget("limitations")
        .and_then(|l| l.jget("breadth"))
        .map_or_else(String::new, |b| json_py(b).py_str());
    PyValue::Map(dict([
        ("scatter", risk_return_scatter(v)),
        ("curves", curves(data)),
        ("robustness", robustness(data, "CAGR")),
        ("timelines", timelines(data)),
        ("subperiods", subperiods(data)),
        ("sharpe", ranked_bars(v, "sharpe", None, 148)),
        ("ablation", ranked_bars(v, "CAGR", Some(&abl), 196)),
        ("families", families_py()),
        ("breadth_caveat", s(&caveat)),
    ]))
}

#[cfg(test)]
mod tests;
