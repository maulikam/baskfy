//! The tax invoice a captured payment raises: `baskfy_core.gst` (the inclusive split),
//! `baskfy_core.pdf` (the deterministic PDF 1.4 writer), `baskfy_core.invoice` (the page) and
//! `baskfy_api.invoices.issue_invoice` (the gapless number, the archive, the `payment` stamp).
//!
//! The PDF is byte-for-byte Python's: Courier at 600/1000 em, positions printed `%.2f` from the
//! same float arithmetic in the same order, the creation date the invoice date at 00:00 IST.

use chrono::{Datelike, NaiveDate};
use rust_decimal::{Decimal, RoundingStrategy};
use sqlx::Row;

use crate::problems::Problem;
use crate::routes::api_keys::model::dbw;
use crate::session::Tx;
use crate::settings::Settings;

/// `A4_WIDTH`.
const A4_WIDTH: f64 = 595.28;
/// `A4_HEIGHT`.
const A4_HEIGHT: f64 = 841.89;
/// `MARGIN`.
const MARGIN: f64 = 48.0;
/// `invoice.DISCLAIMER`.
const DISCLAIMER: &str =
    "Baskfy is not a SEBI-registered investment adviser. Nothing sold here is investment advice.";
/// `REVERSE_CHARGE_NOTE`.
const REVERSE_CHARGE_NOTE: &str = "Whether tax is payable on reverse charge basis: No";

// ---------------------------------------------------------------------------------------------
// baskfy_core.gst
// ---------------------------------------------------------------------------------------------

/// `money(value)`: paise, half-up.
fn money(v: Decimal) -> Decimal {
    v.round_dp_with_strategy(2, RoundingStrategy::MidpointAwayFromZero)
}

/// `GstSplit`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct GstSplit {
    pub(crate) gross: Decimal,
    pub(crate) taxable: Decimal,
    pub(crate) cgst: Decimal,
    pub(crate) sgst: Decimal,
    pub(crate) igst: Decimal,
    pub(crate) rate: Decimal,
    pub(crate) intra_state: bool,
}

impl GstSplit {
    /// `tax_inr`.
    pub(crate) fn tax(&self) -> Decimal {
        self.cgst + self.sgst + self.igst
    }
}

/// `split_inclusive(gross, rate_percent=, intra_state=)`: the taxable value rounded, the tax the
/// remainder.
pub(crate) fn split_inclusive(gross: Decimal, rate: Decimal, intra_state: bool) -> GstSplit {
    let gross = money(gross);
    let taxable = money(gross * Decimal::ONE_HUNDRED / (Decimal::ONE_HUNDRED + rate));
    let tax = gross - taxable;
    let zero = Decimal::new(0, 2);
    let (cgst, sgst, igst) = if intra_state {
        let cgst = money(tax / Decimal::TWO);
        (cgst, tax - cgst, zero)
    } else {
        (zero, zero, tax)
    };
    GstSplit {
        gross,
        taxable,
        cgst,
        sgst,
        igst,
        rate,
        intra_state,
    }
}

/// `financial_year(year, month)`: `2026-27`.
pub(crate) fn financial_year(year: i32, month: u32) -> String {
    let start = if month >= 4 { year } else { year - 1 };
    format!("{start}-{:02}", (start + 1).rem_euclid(100))
}

// ---------------------------------------------------------------------------------------------
// baskfy_core.pdf
// ---------------------------------------------------------------------------------------------

/// `text_width(value, size)`: `len * size * 600 / 1000`, in Python's evaluation order.
fn text_width(value: &str, size: f64) -> f64 {
    #[allow(clippy::cast_precision_loss)]
    let n = value.chars().count() as f64;
    n * size * 600.0 / 1000.0
}

/// `_sanitise`: the transliterations, then anything beyond Latin-1 as `?`.
fn sanitise(value: &str) -> String {
    let mut out = String::with_capacity(value.len());
    for c in value.chars() {
        match c {
            '\u{20b9}' => out.push_str("INR "),
            '\u{2014}' | '\u{2013}' => out.push('-'),
            '\u{2018}' | '\u{2019}' => out.push('\''),
            '\u{201c}' | '\u{201d}' => out.push('"'),
            '\u{00a0}' => out.push(' '),
            c if (c as u32) < 256 => out.push(c),
            _ => out.push('?'),
        }
    }
    out
}

/// `_escape`.
fn escape(value: &str) -> String {
    sanitise(value)
        .replace('\\', "\\\\")
        .replace('(', "\\(")
        .replace(')', "\\)")
}

/// Latin-1 bytes of a string already within it.
fn latin1(s: &str) -> Vec<u8> {
    s.chars()
        .map(|c| u8::try_from(c as u32).unwrap_or(b'?'))
        .collect()
}

struct Text {
    x: f64,
    y: f64,
    value: String,
    size: f64,
    bold: bool,
}

struct Line {
    x1: f64,
    y1: f64,
    x2: f64,
    y2: f64,
    width: f64,
}

/// `Page`.
#[derive(Default)]
struct Page {
    texts: Vec<Text>,
    lines: Vec<Line>,
}

impl Page {
    fn text(&mut self, x: f64, y: f64, value: &str, size: f64, bold: bool) {
        self.texts.push(Text {
            x,
            y,
            value: value.to_owned(),
            size,
            bold,
        });
    }

    fn text_right(&mut self, right: f64, y: f64, value: &str, size: f64, bold: bool) {
        self.text(right - text_width(value, size), y, value, size, bold);
    }

    fn rule(&mut self, x1: f64, y: f64, x2: f64, width: f64) {
        self.lines.push(Line {
            x1,
            y1: y,
            x2,
            y2: y,
            width,
        });
    }

    fn content(&self) -> Vec<u8> {
        let mut parts: Vec<String> = Vec::new();
        for l in &self.lines {
            parts.push(format!(
                "{:.2} w {:.2} {:.2} m {:.2} {:.2} l S",
                l.width, l.x1, l.y1, l.x2, l.y2
            ));
        }
        for t in &self.texts {
            let font = if t.bold { "F2" } else { "F1" };
            parts.push(format!(
                "BT /{font} {:.2} Tf {:.2} {:.2} Td ({}) Tj ET",
                t.size,
                t.x,
                t.y,
                escape(&t.value)
            ));
        }
        latin1(&parts.join("\n"))
    }
}

/// `render_pdf([page], title=, created=)` for one page; `created` is a date at 00:00 IST.
fn render_pdf(page: &Page, title: &str, created: NaiveDate) -> Vec<u8> {
    let mut objects: Vec<Vec<u8>> = Vec::new();
    let add = |objects: &mut Vec<Vec<u8>>, body: Vec<u8>| {
        objects.push(body);
        objects.len()
    };
    let catalog = add(&mut objects, Vec::new());
    let pages = add(&mut objects, Vec::new());
    let regular = add(
        &mut objects,
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>".to_vec(),
    );
    let bold = add(
        &mut objects,
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier-Bold /Encoding /WinAnsiEncoding >>"
            .to_vec(),
    );
    let date = format!(
        "D:{:04}{:02}{:02}000000+05'30'",
        created.year(),
        created.month(),
        created.day()
    );
    let mut info = b"<< /Title (".to_vec();
    info.extend(latin1(&escape(title)));
    info.extend_from_slice(b") /Producer (Baskfy) /Creator (Baskfy) /CreationDate (");
    info.extend(date.as_bytes());
    info.extend_from_slice(b") >>");
    let info = add(&mut objects, info);
    let content = page.content();
    let mut stream = format!("<< /Length {} >>\nstream\n", content.len()).into_bytes();
    stream.extend(&content);
    stream.extend_from_slice(b"\nendstream");
    let content_number = add(&mut objects, stream);
    let page_number = add(
        &mut objects,
        format!(
            "<< /Type /Page /Parent {pages} 0 R /MediaBox [0 0 {A4_WIDTH:.2} {A4_HEIGHT:.2}] \
             /Resources << /Font << /F1 {regular} 0 R /F2 {bold} 0 R >> >> \
             /Contents {content_number} 0 R >>"
        )
        .into_bytes(),
    );
    if let Some(slot) = objects.get_mut(pages - 1) {
        *slot = format!("<< /Type /Pages /Kids [{page_number} 0 R] /Count 1 >>").into_bytes();
    }
    if let Some(slot) = objects.get_mut(catalog - 1) {
        *slot = format!("<< /Type /Catalog /Pages {pages} 0 R >>").into_bytes();
    }
    let mut out = b"%PDF-1.4\n".to_vec();
    out.extend_from_slice(b"%\xe2\xe3\xcf\xd3\n");
    let mut offsets = Vec::with_capacity(objects.len());
    for (index, body) in objects.iter().enumerate() {
        offsets.push(out.len());
        out.extend(format!("{} 0 obj\n", index + 1).into_bytes());
        out.extend(body);
        out.extend_from_slice(b"\nendobj\n");
    }
    let xref = out.len();
    out.extend(format!("xref\n0 {}\n", objects.len() + 1).into_bytes());
    out.extend_from_slice(b"0000000000 65535 f \n");
    for offset in offsets {
        out.extend(format!("{offset:010} 00000 n \n").into_bytes());
    }
    out.extend(
        format!(
            "trailer\n<< /Size {} /Root {catalog} 0 R /Info {info} 0 R >>\nstartxref\n{xref}\n%%EOF\n",
            objects.len() + 1
        )
        .into_bytes(),
    );
    out
}

// ---------------------------------------------------------------------------------------------
// baskfy_core.invoice
// ---------------------------------------------------------------------------------------------

/// `Party`.
pub(crate) struct Party {
    pub(crate) name: String,
    pub(crate) address_lines: Vec<String>,
    pub(crate) gstin: Option<String>,
    pub(crate) state: Option<String>,
    pub(crate) email: Option<String>,
}

/// `InvoiceDocument`.
pub(crate) struct Document {
    pub(crate) invoice_number: String,
    pub(crate) invoice_date: NaiveDate,
    pub(crate) supplier: Party,
    pub(crate) recipient: Party,
    pub(crate) description: String,
    pub(crate) sac_code: String,
    pub(crate) place_of_supply: String,
    pub(crate) tax: GstSplit,
    pub(crate) period: Option<(NaiveDate, NaiveDate)>,
    pub(crate) payment_reference: Option<String>,
}

/// `f"{value:,.2f}"`.
fn amount(v: Decimal) -> String {
    let rounded = v.round_dp_with_strategy(2, RoundingStrategy::MidpointNearestEven);
    let text = format!("{:.2}", rounded.abs());
    let (int, frac) = text.split_once('.').unwrap_or((&text, "00"));
    let mut grouped = String::new();
    for (i, c) in int.chars().enumerate() {
        if i > 0 && (int.len() - i) % 3 == 0 {
            grouped.push(',');
        }
        grouped.push(c);
    }
    let sign = if rounded.is_sign_negative() && !rounded.is_zero() {
        "-"
    } else {
        ""
    };
    format!("{sign}{grouped}.{frac}")
}

/// `_rate(value)`: `f"{value.normalize():f}"`.
fn rate(v: Decimal) -> String {
    v.normalize().to_string()
}

fn party_lines(p: &Party) -> Vec<String> {
    let mut lines = vec![p.name.clone()];
    lines.extend(p.address_lines.iter().cloned());
    if let Some(s) = p.state.as_ref().filter(|s| !s.is_empty()) {
        lines.push(s.clone());
    }
    if let Some(e) = p.email.as_ref().filter(|e| !e.is_empty()) {
        lines.push(e.clone());
    }
    lines.push(match p.gstin.as_ref().filter(|g| !g.is_empty()) {
        Some(g) => format!("GSTIN: {g}"),
        None => "GSTIN: Unregistered".to_owned(),
    });
    lines
}

/// `render_invoice_pdf(document)`.
pub(crate) fn render_invoice_pdf(d: &Document) -> Vec<u8> {
    let mut page = Page::default();
    let right = A4_WIDTH - MARGIN;
    // _draw_header
    let mut y = A4_HEIGHT - MARGIN;
    page.text(MARGIN, y, "TAX INVOICE", 16.0, true);
    page.text_right(right, y, &d.invoice_number, 12.0, true);
    y -= 14.0;
    page.text_right(
        right,
        y,
        &format!("Date: {}", crate::wire::py_date(d.invoice_date)),
        9.0,
        false,
    );
    y -= 12.0;
    page.rule(MARGIN, y, right, 1.0);
    let top = y - 20.0;
    // _draw_parties
    let mut y = top;
    page.text(MARGIN, y, "Supplied by", 9.0, true);
    y -= 13.0;
    for line in party_lines(&d.supplier) {
        page.text(MARGIN, y, &line, 9.0, false);
        y -= 11.0;
    }
    let supplier_bottom = y;
    let middle = MARGIN + (right - MARGIN) / 2.0;
    let mut y = top;
    page.text(middle, y, "Supplied to", 9.0, true);
    y -= 13.0;
    for line in party_lines(&d.recipient) {
        page.text(middle, y, &line, 9.0, false);
        y -= 11.0;
    }
    let mut y = supplier_bottom.min(y) - 8.0;
    page.text(
        MARGIN,
        y,
        &format!("Place of supply: {}", d.place_of_supply),
        9.0,
        false,
    );
    y -= 11.0;
    page.text(MARGIN, y, REVERSE_CHARGE_NOTE, 9.0, false);
    let top = y - 18.0;
    // _draw_line_item
    let mut y = top;
    page.rule(MARGIN, y + 12.0, right, 0.5);
    page.text(MARGIN, y, "Description", 9.0, true);
    page.text(MARGIN + 300.0, y, "SAC", 9.0, true);
    page.text_right(right, y, "Amount", 9.0, true);
    y -= 4.0;
    page.rule(MARGIN, y, right, 0.5);
    y -= 15.0;
    page.text(MARGIN, y, &d.description, 9.0, false);
    page.text(MARGIN + 300.0, y, &d.sac_code, 9.0, false);
    page.text_right(right, y, &amount(d.tax.taxable), 9.0, false);
    y -= 12.0;
    if let Some((start, end)) = d.period {
        page.text(
            MARGIN + 8.0,
            y,
            &format!(
                "Period: {} to {}",
                crate::wire::py_date(start),
                crate::wire::py_date(end)
            ),
            8.0,
            false,
        );
        y -= 12.0;
    }
    y -= 4.0;
    page.rule(MARGIN, y, right, 0.5);
    let top = y - 16.0;
    // _draw_totals
    let mut y = top;
    let half = d.tax.rate / Decimal::TWO;
    let mut rows = vec![("Taxable value".to_owned(), d.tax.taxable)];
    if d.tax.intra_state {
        rows.push((format!("CGST @ {}%", rate(half)), d.tax.cgst));
        rows.push((format!("SGST @ {}%", rate(half)), d.tax.sgst));
    } else {
        rows.push((format!("IGST @ {}%", rate(d.tax.rate)), d.tax.igst));
    }
    for (label, value) in rows {
        page.text_right(right - 90.0, y, &label, 9.0, false);
        page.text_right(right, y, &amount(value), 9.0, false);
        y -= 13.0;
    }
    page.rule(right - 220.0, y + 6.0, right, 0.5);
    y -= 8.0;
    page.text_right(right - 90.0, y, "Total (INR)", 10.0, true);
    page.text_right(right, y, &amount(d.tax.gross), 10.0, true);
    let top = y - 24.0;
    // _draw_footer
    let mut y = top;
    if let Some(reference) = &d.payment_reference {
        page.text(
            MARGIN,
            y,
            &format!("Payment reference: {reference}"),
            8.0,
            false,
        );
        y -= 12.0;
    }
    page.text(
        MARGIN,
        y,
        "Paid online. This is a computer-generated invoice.",
        8.0,
        false,
    );
    page.rule(MARGIN, MARGIN + 22.0, right, 0.5);
    page.text(MARGIN, MARGIN + 10.0, DISCLAIMER, 7.0, false);
    render_pdf(
        &page,
        &format!("Tax invoice {}", d.invoice_number),
        d.invoice_date,
    )
}

// ---------------------------------------------------------------------------------------------
// baskfy_api.invoices
// ---------------------------------------------------------------------------------------------

/// `allocate_invoice_number(session, settings, on=)`: the counter row, created on first use,
/// incremented under its row lock.
pub(crate) async fn allocate_number(
    tx: &mut Tx,
    settings: &Settings,
    on: NaiveDate,
) -> Result<String, Problem> {
    let series = financial_year(on.year(), on.month());
    sqlx::query(
        "INSERT INTO invoice_counter (series, next_value) VALUES ($1, 1) \
         ON CONFLICT (series) DO NOTHING",
    )
    .bind(&series)
    .execute(&mut **tx)
    .await
    .map_err(dbw)?;
    let row = sqlx::query(
        "UPDATE invoice_counter SET next_value = next_value + 1 WHERE series = $1 \
         RETURNING next_value::bigint AS next_value",
    )
    .bind(&series)
    .fetch_one(&mut **tx)
    .await
    .map_err(dbw)?;
    let next: i64 = row.try_get("next_value").map_err(dbw)?;
    let width = usize::try_from(settings.invoice_number_width).unwrap_or(6);
    Ok(format!(
        "{}/{series}/{:0width$}",
        settings.invoice_series_prefix,
        next - 1
    ))
}

/// `describe(plan, period_end)`: "Baskfy <Title-cased code> subscription|plan".
pub(crate) fn describe(code: &str, has_interval: bool) -> String {
    let label = crate::routes::portfolios::portfolio::pyfmt::py_title(&code.replace('_', " "));
    let kind = if has_interval { "subscription" } else { "plan" };
    format!("Baskfy {label} {kind}")
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    #[test]
    fn the_inclusive_split_sums_to_the_gross() {
        // test_gst.py: ₹500 at 18%, intra-state.
        let s = split_inclusive("500.00".parse().unwrap(), Decimal::from(18), true);
        assert_eq!(s.taxable.to_string(), "423.73");
        assert_eq!(s.cgst.to_string(), "38.14");
        assert_eq!(s.sgst.to_string(), "38.13");
        assert_eq!(s.igst.to_string(), "0.00");
        assert_eq!(s.taxable + s.tax(), s.gross);
        let inter = split_inclusive("3999.00".parse().unwrap(), Decimal::from(18), false);
        assert_eq!(inter.igst + inter.taxable, inter.gross);
        assert_eq!(financial_year(2026, 3), "2025-26");
        assert_eq!(financial_year(2026, 4), "2026-27");
        assert_eq!(financial_year(2099, 12), "2099-00");
    }

    #[test]
    fn amounts_and_rates_print_as_python_formats_them() {
        assert_eq!(amount("12711.86".parse().unwrap()), "12,711.86");
        assert_eq!(amount("423.73".parse().unwrap()), "423.73");
        assert_eq!(amount("1234567.5".parse().unwrap()), "1,234,567.50");
        assert_eq!(rate(Decimal::from(9)), "9");
        assert_eq!(rate("18.00".parse().unwrap()), "18");
        assert_eq!(rate("6.25".parse().unwrap()), "6.25");
        assert_eq!(describe("forever", false), "Baskfy Forever plan");
        assert_eq!(
            describe("pro_monthly", true),
            "Baskfy Pro Monthly subscription"
        );
    }

    #[test]
    fn the_pdf_is_pythons_byte_for_byte() {
        // `render_invoice_pdf` on this document, dumped from Python (invoice_golden.pdf).
        let d = Document {
            invoice_number: "DCL/2026-27/000007".to_owned(),
            invoice_date: NaiveDate::from_ymd_opt(2026, 10, 30).unwrap(),
            supplier: Party {
                name: "Baskfy (Test) Pvt \\ Ltd".to_owned(),
                address_lines: vec!["1 MG Road".to_owned(), "Bengaluru 560001".to_owned()],
                gstin: Some("29ABCDE1234F1Z5".to_owned()),
                state: Some("Karnataka (29)".to_owned()),
                email: None,
            },
            recipient: Party {
                name: "Renu — ₹ “Dave” ✓".to_owned(),
                address_lines: Vec::new(),
                gstin: None,
                state: None,
                email: Some("renu@example.com".to_owned()),
            },
            description: "Baskfy Monthly subscription".to_owned(),
            sac_code: "998439".to_owned(),
            place_of_supply: "Karnataka (29)".to_owned(),
            tax: split_inclusive("1234567.00".parse().unwrap(), Decimal::from(18), true),
            period: Some((
                NaiveDate::from_ymd_opt(2026, 10, 1).unwrap(),
                NaiveDate::from_ymd_opt(2026, 11, 1).unwrap(),
            )),
            payment_reference: Some("pay_ABC123".to_owned()),
        };
        let want = include_bytes!("invoice_golden.pdf");
        let got = render_invoice_pdf(&d);
        assert_eq!(String::from_utf8_lossy(&got), String::from_utf8_lossy(want));
        assert_eq!(got.as_slice(), want.as_slice());
    }
}
