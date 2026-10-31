use crate::wire::PyValue;

fn float_cell(x: f64) -> String {
    if x.is_nan() {
        return " NaN".to_owned();
    }
    if x.is_infinite() {
        return if x > 0.0 { " inf" } else { "-inf" }.to_owned();
    }
    let mut s = format!("{x:.6}");
    if !s.starts_with('-') {
        s.insert(0, ' ');
    }
    let mut t = s.trim_end_matches('0').to_owned();
    if t.ends_with('.') {
        t.push('0');
    }
    t
}

fn cell(v: &PyValue) -> String {
    match v {
        PyValue::Float(x) => float_cell(*x),
        PyValue::Int(i) => format!(" {i}"),
        PyValue::Bool(b) => format!(" {}", if *b { "True" } else { "False" }),
        PyValue::None => " None".to_owned(),
        other => format!(" {}", other.py_str()),
    }
}

fn width(s: &str) -> usize {
    s.chars().count()
}

fn rjust(s: &str, w: usize) -> String {
    format!("{}{s}", " ".repeat(w.saturating_sub(width(s))))
}

fn ljust(s: &str, w: usize) -> String {
    format!("{s}{}", " ".repeat(w.saturating_sub(width(s))))
}

/// `DataFrame.to_string()` for an object-dtype frame: `rows[i][j]` is row `i`, column `j`.
pub(super) fn to_string(index: &[String], columns: &[&str], rows: &[Vec<PyValue>]) -> String {
    let iw = index.iter().map(|s| width(s)).max().unwrap_or(0);
    let cols: Vec<(Vec<String>, usize)> = columns
        .iter()
        .enumerate()
        .map(|(j, h)| {
            let cells: Vec<String> = rows
                .iter()
                .map(|r| r.get(j).map_or_else(|| float_cell(f64::NAN), cell))
                .collect();
            let w = cells
                .iter()
                .map(|c| width(c))
                .chain([width(h)])
                .max()
                .unwrap_or(0);
            (cells, w)
        })
        .collect();
    let mut lines = Vec::with_capacity(rows.len() + 1);
    let mut head = " ".repeat(iw);
    for (h, (_, w)) in columns.iter().zip(&cols) {
        head.push(' ');
        head.push_str(&rjust(h, *w));
    }
    lines.push(head);
    for (i, name) in index.iter().enumerate() {
        let mut line = ljust(name, iw);
        for (cells, w) in &cols {
            line.push(' ');
            line.push_str(&rjust(&cells[i], *w));
        }
        lines.push(line);
    }
    lines.join("\n")
}
