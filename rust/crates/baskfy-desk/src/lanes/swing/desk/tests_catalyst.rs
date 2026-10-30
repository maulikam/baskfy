//! `tests/test_swing_catalyst_desk.py`: SW11B on the desk page — the catalyst is a link on
//! triggers and plan lines, never text; the store reads the newest announcement and the
//! earnings date per name.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use super::scenario::{Client, Scenario, SignalSpec, USER, desk_root, ist_at};
use super::*;
use crate::lanes::swing::store::PgSwingStore;
use crate::storage::Param;

const FILING: &str =
    "https://nsearchives.nseindia.com/corporate/ALPHAFLAG_02092026084105_PR.pdf?x=1&y=2";
const CALENDAR: &str =
    "https://www.nseindia.com/companies-listing/corporate-filings-event-calendar?symbol=ALPHAFLAG";

async fn catalyst(
    sc: &mut Scenario,
    instrument_id: i64,
    headline: &str,
    url: &str,
    published_at: Option<DateTime<FixedOffset>>,
    source: &str,
    earnings_date: Option<NaiveDate>,
) {
    sc.exec(
        "INSERT INTO sw_catalyst(user_id, instrument_id, headline, published_at, url, source, \
         earnings_date) VALUES (?, ?, ?, ?, ?, ?, ?)",
        &[
            Param::Int(USER),
            Param::Int(instrument_id),
            Param::Text(headline.to_owned()),
            published_at.map_or(Param::Null, |t| Param::Text(t.to_rfc3339())),
            Param::Text(url.to_owned()),
            Param::Text(source.to_owned()),
            earnings_date.map_or(Param::Null, |d| {
                Param::Text(d.format("%Y-%m-%d").to_string())
            }),
        ],
    )
    .await;
}

fn token() -> Rec {
    Rec::of([
        ("present", Val::Bool(false)),
        ("label", Val::from("-")),
        ("age_minutes", Val::None),
        ("expired", Val::Bool(true)),
    ])
}

async fn view(store: &mut PgSwingStore) -> serde_json::Value {
    let flags = ViewFlags {
        dry_run: true,
        execution_enabled: false,
        monitor_enabled: false,
        token: token(),
    };
    let v = build_view(store, super::scenario::now(), &flags)
        .await
        .unwrap();
    let text = crate::wire::orjson_dumps(&PyValue::Map(jsonable_rec(&v))).unwrap();
    serde_json::from_str(&text).unwrap()
}

#[tokio::test]
async fn test_the_newest_announcement_and_the_earnings_date_per_name() {
    let mut sc = Scenario::new().await;
    sc.config().await;
    let older = ist_at(2026, 9, 1, 18, 32) + chrono::Duration::seconds(11);
    let newest = ist_at(2026, 9, 2, 8, 41) + chrono::Duration::seconds(5);
    let ann = "NSE_ANNOUNCEMENT";
    catalyst(
        &mut sc,
        1,
        "Updates — capex",
        "https://a/older.pdf",
        Some(older),
        ann,
        None,
    )
    .await;
    catalyst(
        &mut sc,
        1,
        "Press Release - deal",
        FILING,
        Some(newest),
        ann,
        None,
    )
    .await;
    catalyst(
        &mut sc,
        1,
        "No stamp",
        "https://a/undated.pdf",
        None,
        ann,
        None,
    )
    .await;
    let earnings = NaiveDate::from_ymd_opt(2026, 10, 15).unwrap();
    catalyst(
        &mut sc,
        1,
        "Financial Results",
        CALENDAR,
        None,
        "NSE_EVENT_CALENDAR",
        Some(earnings),
    )
    .await;
    let got = sc.store().await.catalysts_for(&[1, 2, 1]).await.unwrap();
    assert_eq!(got.keys().copied().collect::<Vec<_>>(), [1]);
    let c = &got[&1];
    assert_eq!(c.headline.as_deref(), Some("Press Release - deal"));
    assert_eq!(c.published_at, Some(newest));
    assert_eq!(c.url.as_deref(), Some(FILING));
    assert_eq!(c.earnings_date, Some(earnings));
}

#[tokio::test]
async fn test_no_names_is_no_query() {
    let mut sc = Scenario::new().await;
    sc.config().await;
    assert!(
        sc.store()
            .await
            .catalysts_for(&[])
            .await
            .unwrap()
            .is_empty()
    );
}

#[tokio::test]
async fn test_a_trigger_carries_its_catalyst_as_a_new_tab_link_and_an_earnings_badge() {
    let mut sc = Scenario::new().await;
    sc.config().await;
    let newest = ist_at(2026, 9, 2, 8, 41) + chrono::Duration::seconds(5);
    catalyst(
        &mut sc,
        1,
        "Press Release - deal",
        FILING,
        Some(newest),
        "NSE_ANNOUNCEMENT",
        None,
    )
    .await;
    catalyst(
        &mut sc,
        1,
        "Financial Results",
        CALENDAR,
        None,
        "NSE_EVENT_CALENDAR",
        NaiveDate::from_ymd_opt(2026, 10, 15),
    )
    .await;
    sc.signal(SignalSpec::new(
        1,
        "FLAG",
        "TRIGGERED",
        ist_at(2026, 9, 2, 9, 31),
    ))
    .await;
    sc.signal(SignalSpec::new(
        5,
        "FLAG",
        "BELOW_PIVOT",
        ist_at(2026, 9, 2, 9, 35),
    ))
    .await;
    let j = view(&mut sc.store().await).await;
    let by_symbol: std::collections::BTreeMap<String, serde_json::Value> = j["triggers"]
        .as_array()
        .unwrap()
        .iter()
        .map(|t| {
            (
                t["symbol"].as_str().unwrap().to_owned(),
                t["catalyst"].clone(),
            )
        })
        .collect();
    assert_eq!(by_symbol["ALPHAFLAG"]["url"], FILING);
    assert!(by_symbol["EPSILON"].is_null());

    let html = Client::new(&sc).await.html("/swing").await;
    assert!(html.contains(&format!(
        r#"<a class="sw-catalyst" href="{}" target="_blank" rel="noopener noreferrer""#,
        FILING.replace('&', "&amp;")
    )));
    assert!(html.contains("Press Release - deal</a>"));
    assert!(html.contains("earnings 15 Oct"));
    assert!(!html.contains("capex"));
}

#[tokio::test]
async fn test_a_plan_line_carries_the_link_and_a_name_without_one_shows_a_dash() {
    let mut sc = Scenario::new().await;
    let ids = sc.morning().await;
    let newest = ist_at(2026, 9, 2, 8, 41) + chrono::Duration::seconds(5);
    catalyst(
        &mut sc,
        1,
        "Press Release - deal",
        FILING,
        Some(newest),
        "NSE_ANNOUNCEMENT",
        None,
    )
    .await;
    let j = view(&mut sc.store().await).await;
    let buys: Vec<(String, serde_json::Value)> = j["plans"]["morning"]["buys"]
        .as_array()
        .unwrap()
        .iter()
        .map(|l| {
            (
                l["symbol"].as_str().unwrap().to_owned(),
                l["catalyst"].clone(),
            )
        })
        .collect();
    let alpha = buys.iter().find(|(s, _)| s == "ALPHAFLAG").unwrap();
    assert_eq!(alpha.1["headline"], "Press Release - deal");
    assert!(
        buys.iter()
            .filter(|(s, _)| s != "ALPHAFLAG")
            .all(|(_, c)| c.is_null())
    );
    assert!(ids.morning.0 > 0);

    let html = Client::new(&sc).await.html("/swing").await;
    assert!(html.matches(r#"class="sw-catalyst""#).count() >= 1);
    assert!(html.contains("opens on nseindia.com"));
}

#[test]
fn test_the_page_names_no_placing_verb_for_the_feed() {
    let template = std::fs::read_to_string(desk_root().join("app/templates/swing.html")).unwrap();
    let macro_body = template
        .split_once("{% macro catalyst(c) -%}")
        .unwrap()
        .1
        .split_once("{%- endmacro %}")
        .unwrap()
        .0;
    assert!(!macro_body.contains("<form") && !macro_body.contains("/swing/execute"));
    assert!(macro_body.contains(r#"target="_blank" rel="noopener noreferrer""#));
}
