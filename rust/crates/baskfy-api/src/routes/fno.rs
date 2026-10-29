//! ``/fno/*`` — the FO run's read surface for the web app (FO5). Ports `baskfy_api.routers.fno`
//! (`APIRouter(prefix="/fno", tags=["fno"])`) and the reads it calls (`baskfy_api.fno_read`,
//! the read half of `baskfy_api.fno_settings`), in `routes/derivatives/fno_read.rs`.
//!
//! Ported (the derivatives lane, RS5): `GET /overnight`, `GET /info`, `GET /config`.
//!
//! **Not registered:** `PATCH /config` — the one money-free write. The pool is read-only
//! (DECISIONS-RS R5.2), so Python keeps serving it until `fo_book_config`/`fo_sleeve_config`/
//! `fo_config_audit` are writable from Rust.
//!
//! READ-ONLY WHERE MONEY IS CONCERNED: there is no execute, confirm, plan or close route here
//! and there never will be (law 2; the desk's `/fno` is the only surface with a Confirm). The FO
//! execution flags are read only to *report* them (PAPER / LIVE per sleeve group). Nothing here
//! reaches Kite. Every number is end of day (root CLAUDE.md's two-clock table, the Overnight and
//! Stock F&O rows): `scan_date` is the bhavcopy the scan read, marks are at that session's
//! settle, `as_of` is the series' latest session.

use std::collections::BTreeMap;

use axum::Router;
use axum::extract::{Extension, State};
use axum::response::Response;
use axum::routing::get;
use baskfy_db::Db;
use chrono::NaiveDate;

use super::options::derivatives::fno_read::{
    self as reads, F1_SLEEVES, F1_UNDERLYINGS, F2_SLEEVES, F3_SLEEVES, F3_UNDERLYINGS,
    FnoBacktestOut, FnoCeilingsOut, FnoConfigOut, FnoEvidenceOut, FnoF2Out, FnoF3Out, FnoGateOut,
    FnoInfoOut, FnoOvernightOut, FnoUnderlyingOut,
};
use super::options::derivatives::shared::{now_ist, scoped_sole_user_id};
use crate::auth::{Principal, require_authenticated};
use crate::problems::Problem;
use crate::settings::Settings;
use crate::state::AppState;
use crate::wire;

/// `_gates(settings)`: reported, never branched on.
fn gates(settings: &Settings) -> Vec<FnoGateOut> {
    [
        ("F1", settings.fno_f1_execution_enabled),
        ("F2", settings.fno_f2_execution_enabled),
        ("F3", settings.fno_f3_execution_enabled),
    ]
    .into_iter()
    .map(|(group, enabled)| FnoGateOut {
        group,
        mode: if enabled { "LIVE" } else { "PAPER" },
    })
    .collect()
}

/// One underlying's card: its level, the night's scan row and the next entry the scan named
/// (`first_key` or else `second_key` of the row's `detail`).
async fn underlying(
    db: &Db,
    symbol: &str,
    sleeve: &str,
    day: Option<NaiveDate>,
    scan: Option<reads::FnoScanOut>,
    keys: (&str, &str),
) -> Result<FnoUnderlyingOut, Problem> {
    let entry = match &scan {
        None => None,
        Some(s) => {
            let first = s.detail.get(keys.0);
            let truthy_first = reads::iso_date_of(first)?;
            match truthy_first {
                Some(d) => Some(d),
                None => reads::iso_date_of(s.detail.get(keys.1))?,
            }
        }
    };
    Ok(FnoUnderlyingOut {
        symbol: symbol.to_owned(),
        sleeve: sleeve.to_owned(),
        level: reads::underlying_level(db, symbol, day).await?,
        scan,
        next_entry_date: entry,
    })
}

/// `_f3(session, user_id, today, backtests)`.
async fn f3(
    db: &Db,
    user_id: i64,
    today: NaiveDate,
    backtests: &[FnoBacktestOut],
) -> Result<FnoF3Out, Problem> {
    let f3_date = reads::latest_scan_date(db, user_id, &F3_SLEEVES).await?;
    let mut rows: BTreeMap<String, reads::FnoScanOut> = BTreeMap::new();
    if let Some(d) = f3_date {
        for r in reads::scan_rows(db, user_id, d, &F3_SLEEVES).await? {
            rows.insert(r.symbol.clone(), r);
        }
    }
    let mut underlyings = Vec::new();
    for (symbol, sleeve) in F3_UNDERLYINGS {
        let scan = rows.remove(symbol);
        underlyings.push(
            underlying(
                db,
                symbol,
                sleeve,
                f3_date,
                scan,
                ("entry_session", "next_session"),
            )
            .await?,
        );
    }
    Ok(FnoF3Out {
        research_line: reads::RESEARCH_F3_LINE,
        not_tested: baskfy_core::fno::directional_retest::NOT_TESTED.to_vec(),
        scan_date: f3_date,
        underlyings,
        open: reads::open_positions(db, user_id, today, &F3_SLEEVES).await?,
        closed: reads::journal(db, user_id, &F3_SLEEVES).await?,
        backtests: backtests
            .iter()
            .filter(|b| reads::F3_FAMILIES.contains(&b.family.as_str()))
            .cloned()
            .collect(),
    })
}

/// `GET /fno/overnight`: `05` §2 in one call — the header, the F1 cards, the evidence card, the
/// F2 section and the F3 section.
///
/// # Errors
/// 401 anonymous; 404 a caller who is not the sole tenant; 503 no sole tenant configured; 500 a
/// database failure.
pub async fn get_fno_overnight(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let db = &state.db;
    let settings = &state.settings;
    let user_id = scoped_sole_user_id(db, principal.user_id, None).await?;
    let today = now_ist().date_naive();
    let next_session = reads::next_session_after(db, today).await?;

    let f1_date = reads::latest_scan_date(db, user_id, &F1_SLEEVES).await?;
    let mut f1_rows: BTreeMap<String, reads::FnoScanOut> = BTreeMap::new();
    if let Some(d) = f1_date {
        for r in reads::scan_rows(db, user_id, d, &F1_SLEEVES).await? {
            f1_rows.insert(r.symbol.clone(), r);
        }
    }
    let mut underlyings = Vec::new();
    for (symbol, sleeve) in F1_UNDERLYINGS {
        let scan = f1_rows.remove(symbol);
        underlyings.push(
            underlying(
                db,
                symbol,
                sleeve,
                f1_date,
                scan,
                ("next_entry_date", "entry_session"),
            )
            .await?,
        );
    }

    let structures = reads::open_positions(db, user_id, today, &F1_SLEEVES).await?;
    let f2_date = reads::latest_scan_date(db, user_id, &F2_SLEEVES).await?;
    let f2_rows = match f2_date {
        None => Vec::new(),
        Some(d) => reads::scan_rows(db, user_id, d, &F2_SLEEVES).await?,
    };
    let mut counts: BTreeMap<String, i64> = BTreeMap::new();
    for r in &f2_rows {
        *counts.entry(r.state.clone()).or_insert(0) += 1;
    }
    let whole = f2_rows
        .iter()
        .find(|r| r.symbol == reads::F2_WHOLE_SLEEVE_SYMBOL)
        .cloned();

    let backtests = reads::latest_backtests(db, user_id).await?;
    let f3 = f3(db, user_id, today, &backtests).await?;

    let empty_reason =
        (f1_date.is_none() && f2_date.is_none() && f3.scan_date.is_none()).then(|| {
            if settings.fno_scan_enabled {
                reads::EMPTY_NEVER
            } else {
                reads::EMPTY_SCAN_OFF
            }
        });
    let journal = reads::journal(db, user_id, &F1_SLEEVES).await?;
    let tally = reads::paper_tally(db, user_id).await?;
    let f2_open = reads::open_positions(db, user_id, today, &F2_SLEEVES).await?;
    let f2_closed = reads::journal(db, user_id, &F2_SLEEVES).await?;
    let view = FnoOvernightOut {
        today,
        next_session: Some(next_session),
        scan_date: f1_date,
        empty_reason,
        scan_enabled: settings.fno_scan_enabled,
        monitor_enabled: settings.fno_monitor_enabled,
        gates: gates(settings),
        underlyings,
        hard_exit_tomorrow: structures
            .iter()
            .filter(|p| p.hard_exit_date == Some(next_session))
            .cloned()
            .collect(),
        open_structures: structures,
        journal,
        evidence: FnoEvidenceOut {
            tier: "2E",
            line: reads::RESEARCH_B4_LINE,
            loss_close_line: reads::RESEARCH_B4_LOSS_CLOSE_LINE,
            slippage_note: reads::RESEARCH_B4_SLIPPAGE,
            caveat: reads::TIER_2E_CAVEAT,
            n: reads::RESEARCH_B4_N,
            run_date: reads::research_run_date(),
            sample: reads::RESEARCH_SAMPLE,
            why_paper: reads::RESEARCH_B4_WHY_PAPER.to_vec(),
            backtests: backtests
                .iter()
                .filter(|b| b.family == "B4" || b.family == "B4_LOSS_CLOSE")
                .cloned()
                .collect(),
            tally,
        },
        f2: FnoF2Out {
            research_line: reads::RESEARCH_F2_LINE,
            scan_date: f2_date,
            candidates: f2_rows
                .iter()
                .filter(|r| {
                    reads::F2_LISTED_STATES.contains(&r.state.as_str())
                        && r.symbol != reads::F2_WHOLE_SLEEVE_SYMBOL
                })
                .cloned()
                .collect(),
            sleeve_row: whole,
            state_counts: counts,
            open: f2_open,
            closed: f2_closed,
        },
        f3,
    };
    Ok(wire::json(&view))
}

/// `GET /fno/info`: `04` §5 and `05` §3 — facts per F&O underlying, never a candidate.
///
/// # Errors
/// As [`get_fno_overnight`].
pub async fn get_fno_info(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let db = &state.db;
    let user_id = scoped_sole_user_id(db, principal.user_id, None).await?;
    let today = now_ist().date_naive();
    let as_of = reads::latest_series_date(db).await?;
    let rows = match as_of {
        None => Vec::new(),
        Some(d) => reads::info_rows(db, d).await?,
    };
    let backtests = reads::latest_backtests(db, user_id).await?;
    Ok(wire::json(&FnoInfoOut {
        as_of,
        rows,
        families: reads::families(&backtests),
        research_run_date: reads::research_run_date(),
        research_sample: reads::RESEARCH_SAMPLE,
        spread_sample: reads::spread_summary(db).await?,
        ingest: reads::ingest_summary(db, today).await?,
    }))
}

/// `GET /fno/config`: the settings, the server's ceilings and the audit trail.
///
/// # Errors
/// As [`get_fno_overnight`].
pub async fn get_fno_config(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let db = &state.db;
    let settings = &state.settings;
    let user_id = scoped_sole_user_id(db, principal.user_id, None).await?;
    let (book, sleeves) = match reads::read_config(db, user_id).await? {
        Some((book, sleeves)) => (Some(book), sleeves),
        None => (None, Vec::new()),
    };
    Ok(wire::json(&FnoConfigOut {
        seeded: book.is_some(),
        book,
        sleeves,
        ceilings: FnoCeilingsOut {
            risk_per_trade_inr_max: settings.fno_risk_per_trade_inr_max,
            risk_pct_max: settings.fno_risk_pct_max,
            max_open_positions_max: settings.fno_max_open_positions_max,
            max_per_underlying_max: settings.fno_max_per_underlying_max,
            book_monthly_loss_inr_max: settings.fno_book_monthly_loss_inr_max,
        },
        gates: gates(settings),
        audit: reads::audit_trail(db, "fo_config_audit", user_id).await?,
    }))
}

/// The ported `/fno` routes, relative to `/api/v1` (`PATCH /fno/config` stays on Python).
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/fno/overnight", get(get_fno_overnight))
        .route("/fno/info", get(get_fno_info))
        .route("/fno/config", get(get_fno_config))
}
