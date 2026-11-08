//! `baskfy_core.universes` (the fifteen selectable universes) and `baskfy_core.ranking_presets`
//! (the named presets, with `baskfy_core.ranking.RANKING_PRESETS`' descriptions) — reference
//! data the API publishes verbatim, ported as data.

use baskfy_db::OrderedJson;

/// One `Universe`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct Universe {
    /// `index_id` (also the `universe_mask` bit + 1).
    pub index_id: i64,
    /// `slug`.
    pub slug: &'static str,
    /// `name`.
    pub name: &'static str,
    /// `ui_order`.
    pub ui_order: i64,
    /// `market_health`.
    pub market_health: bool,
}

const fn u(
    index_id: i64,
    slug: &'static str,
    name: &'static str,
    ui_order: i64,
    market_health: bool,
) -> Universe {
    Universe {
        index_id,
        slug,
        name,
        ui_order,
        market_health,
    }
}

/// `UNIVERSES`, in `index_id` order.
pub(crate) const UNIVERSES: [Universe; 15] = [
    u(1, "nifty-50", "NIFTY 50", 1, true),
    u(2, "nifty-next-50", "NIFTY NEXT 50", 2, true),
    u(3, "nifty-100", "NIFTY 100", 3, true),
    u(4, "nifty-200", "NIFTY 200", 4, true),
    u(5, "nifty-500", "NIFTY 500", 5, true),
    u(6, "nifty-total-market", "NIFTY TOTAL MARKET", 6, true),
    u(7, "nifty-large-mid-250", "NIFTY LARGE MID 250", 7, true),
    u(8, "nifty-midcap-150", "NIFTY MIDCAP 150", 8, true),
    u(9, "nifty-smallcap-250", "NIFTY SMALLCAP 250", 9, true),
    u(10, "nifty-microcap-250", "NIFTY MICROCAP 250", 10, true),
    u(11, "nifty-mid-small-400", "NIFTY MID SMALL 400", 11, true),
    u(12, "nifty-allcap", "All NSE Listed Stocks", 13, true),
    u(13, "nifty-fno", "NIFTY FNO", 12, false),
    u(14, "etf", "All NSE Listed ETFs", 14, false),
    u(15, "nse-sme-emerge", "NSE SME (Emerge)", 15, false),
];

/// `UNIVERSE_BY_SLUG.get(slug)`.
pub(crate) fn by_slug(slug: &str) -> Option<Universe> {
    UNIVERSES.iter().copied().find(|u| u.slug == slug)
}

/// `MARKET_HEALTH_SLUGS`, as `sorted(...)` of it (the router's error message uses the sorted
/// list's `repr`).
pub(crate) fn market_health_slugs_sorted() -> Vec<&'static str> {
    let mut slugs: Vec<&'static str> = UNIVERSES
        .iter()
        .filter(|u| u.market_health)
        .map(|u| u.slug)
        .collect();
    slugs.sort_unstable();
    slugs
}

/// `repr(list_of_str)` for ASCII strings without quotes inside.
pub(crate) fn py_list_repr(items: &[&str]) -> String {
    let inner: Vec<String> = items
        .iter()
        .map(|s| baskfy_providers::py::repr_str(s))
        .collect();
    format!("[{}]", inner.join(", "))
}

// ---------------------------------------------------------------------------------------------
// Ranking presets
// ---------------------------------------------------------------------------------------------

/// `DESK_SCORE_KEY`.
const DESK_SCORE_KEY: &str = "desk_score";

fn s(x: &str) -> OrderedJson {
    OrderedJson::Str(x.to_owned())
}

fn obj(entries: Vec<(&str, OrderedJson)>) -> OrderedJson {
    OrderedJson::Object(
        entries
            .into_iter()
            .map(|(k, v)| (k.to_owned(), v))
            .collect(),
    )
}

/// `_EXTRA_OFF`.
fn extra_off() -> OrderedJson {
    obj(vec![
        ("enabled", OrderedJson::Bool(false)),
        ("sort_by", OrderedJson::Null),
        ("sort_direction", s("desc")),
    ])
}

/// `_term(factor, preference, **bounds)`.
fn term(factor: &str, preference: &str, bounds: &[(&str, f64)]) -> OrderedJson {
    let mut entries = vec![("factor", s(factor)), ("preference", s(preference))];
    for (k, v) in bounds {
        entries.push((k, OrderedJson::Float(*v)));
    }
    obj(entries)
}

/// `_ranked_by_terms(mode, *terms)`.
fn ranked_by_terms(mode: &str, terms: Vec<OrderedJson>) -> Vec<(&'static str, OrderedJson)> {
    let first = terms
        .first()
        .and_then(|t| t.get("factor"))
        .cloned()
        .unwrap_or(OrderedJson::Null);
    vec![
        ("sort_by", first),
        ("sort_direction", s("desc")),
        ("ranking_mode", s(mode)),
        ("ranking_scope", s("filtered_results")),
        ("ranking_terms", OrderedJson::Array(terms)),
        ("family_weights", OrderedJson::Null),
        ("missing_data", s("penalize")),
        ("factor_two", extra_off()),
        ("factor_three", extra_off()),
    ]
}

/// One `PresetSpec` as `list_presets()` and the route read it.
#[derive(Debug, Clone)]
pub(crate) struct Preset {
    /// `key`.
    pub key: &'static str,
    /// `label`.
    pub label: &'static str,
    /// `description` (`RANKING_PRESETS[key]`).
    pub description: &'static str,
    /// `status`.
    pub status: &'static str,
    /// `patch`.
    pub patch: OrderedJson,
}

impl Preset {
    /// `str(spec.patch["sort_by"])`.
    pub(crate) fn sort_by(&self) -> String {
        match self.patch.get("sort_by") {
            Some(OrderedJson::Str(x)) => x.clone(),
            _ => "None".to_owned(),
        }
    }
}

/// `PRESET_SPECS`, in order.
pub(crate) fn presets() -> Vec<Preset> {
    let mut nse = ranked_by_terms("single", vec![term("nse_momentum_score", "higher", &[])]);
    nse.push(("index", s("nifty-200")));
    vec![
        Preset {
            key: "desk_quality",
            label: "Desk quality",
            description: "Weekly book's Momentum Quality Score (A-F) via baskfy_core.score",
            status: "ready",
            patch: obj(vec![
                ("sort_by", s(DESK_SCORE_KEY)),
                ("sort_direction", s("desc")),
                ("ranking_mode", s("single")),
                ("ranking_scope", s("filtered_results")),
                ("factor_two", extra_off()),
                ("factor_three", extra_off()),
            ]),
        },
        Preset {
            key: "path_quality",
            label: "Path quality",
            description: "Composite of positive days (6M) paired with max drawdown (1Y), downside \
                          volatility (1Y) and the 1Y return without its three best days",
            status: "research",
            patch: obj(ranked_by_terms(
                "composite",
                vec![
                    term("pos_days_6m", "higher", &[]),
                    term("max_dd_12m", "lower", &[]),
                    term("downside_vol_12m", "lower", &[]),
                    term("ret_ex_top3_12m", "higher", &[]),
                ],
            )),
        },
        Preset {
            key: "trend_structure",
            label: "Trend structure",
            description: "Composite of the MA stack and the MA 50 slope, with ATR extension from \
                          the 20-day MA scored as a 0-3 ATR target range rather than \
                          higher-is-better",
            status: "research",
            patch: obj(ranked_by_terms(
                "composite",
                vec![
                    term("ma_stack_score", "higher", &[]),
                    term("ma50_slope_20", "higher", &[]),
                    term(
                        "atr_ext_20",
                        "target_range",
                        &[("target_min", 0.0), ("target_max", 3.0)],
                    ),
                ],
            )),
        },
        Preset {
            key: "participation",
            label: "Participation",
            description: "Composite of 21-session traded value over its 126-session baseline and \
                          how many of the last 20 sessions traded above that baseline",
            status: "research",
            patch: obj(ranked_by_terms(
                "composite",
                vec![
                    term("vol_exp_21_126", "higher", &[]),
                    term("vol_persist_20", "higher", &[]),
                ],
            )),
        },
        Preset {
            key: "leadership",
            label: "Leadership",
            description: "Composite of momentum-rank persistence (20 days), relative-strength \
                          persistence against NIFTY 500 (126 sessions) and average Sharpe \
                          12/6/3/1M",
            status: "research",
            patch: obj(ranked_by_terms(
                "composite",
                vec![
                    term("rank_persist_20", "higher", &[]),
                    term("rs_persist_126", "higher", &[]),
                    term("avg_sharpe_12_6_3_1", "higher", &[]),
                ],
            )),
        },
        Preset {
            key: "nse_momentum",
            label: "NIFTY200 Momentum 30 score (NSE methodology)",
            description: "NSE's NIFTY200 Momentum 30 score and eligibility on NIFTY 200: 0.5 Z12 \
                          + 0.5 Z6 of the 6M and 12M momentum ratios, F&O names with a year of \
                          history. Score and eligibility only: free-float market-cap weights, \
                          the 5% stock cap and the 15/45 rank buffer are index construction and \
                          are not applied, so this is not the index",
            status: "research",
            patch: obj(nse),
        },
        Preset {
            key: "desk_sequential",
            label: "Desk sequential",
            description: "Sequential: desk score, then the lower ATR extension from the 20-day \
                          MA. C7's third tie-break, median traded value (median_vol_12m), is not \
                          a registry factor, so it is not applied",
            status: "research",
            patch: obj(ranked_by_terms(
                "sequential",
                vec![
                    term(DESK_SCORE_KEY, "higher", &[]),
                    term("atr_ext_20", "lower", &[]),
                ],
            )),
        },
    ]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fourteen_ids_are_dense_and_twelve_have_breadth() {
        for (i, u) in UNIVERSES.iter().enumerate() {
            assert_eq!(u.index_id, i64::try_from(i).unwrap_or(-1) + 1);
        }
        assert_eq!(UNIVERSES.iter().filter(|u| u.market_health).count(), 12);
        assert!(by_slug("nifty-fno").is_some_and(|u| !u.market_health));
        assert!(by_slug("nope").is_none());
    }

    #[test]
    fn the_health_list_repr_is_pythons() {
        let slugs = market_health_slugs_sorted();
        assert_eq!(slugs.first(), Some(&"nifty-100"));
        assert!(py_list_repr(&slugs).starts_with("['nifty-100', 'nifty-200', "));
    }
}
