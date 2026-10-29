//! What a caller may do. Ports `baskfy_api.entitlements` and the shape in
//! `baskfy_core.entitlements` (docs/07 §Entitlements).
//!
//! One resolution path, as in Python: the plan behind the caller's paying subscription sets the
//! baseline (or `REGISTERED` — the ₹0 tier when `BASKFY_FREE_TIER_ENABLED`), then the staff
//! overrides in force are layered on top. The `/me` payload is exactly docs/07's seven keys.

use std::collections::BTreeSet;

use baskfy_db::{Db, OrderedJson, row};
use chrono::{DateTime, Utc};
use serde::Serialize;
use sqlx::Row;

use crate::auth::Principal;
use crate::problems::Problem;
use crate::settings::Settings;

/// `Feature`, in docs/07's order.
pub const FEATURES: [&str; 6] = [
    "screener",
    "export_csv",
    "custom_columns",
    "historical_ranks",
    "backtests",
    "api_access",
];

/// `MAX_SCREENS_KEY`.
pub const MAX_SCREENS_KEY: &str = "max_screens";
/// `ENTITLEMENTS_KEY`.
pub const ENTITLEMENTS_KEY: &str = "entitlements";
/// `UNIVERSES_KEY`.
pub const UNIVERSES_KEY: &str = "universes";
/// `PAID_MAX_SCREENS`.
pub const PAID_MAX_SCREENS: i64 = 50;
/// `FREE_MAX_SCREENS`.
pub const FREE_MAX_SCREENS: i64 = 5;
/// `ENTITLING_STATUSES`: only `active` entitles.
pub const ENTITLING_STATUSES: [&str; 1] = ["active"];

/// `Entitlements`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Entitlements {
    /// `granted` (feature values).
    pub granted: BTreeSet<&'static str>,
    /// `max_screens`.
    pub max_screens: i64,
    /// `universes` (`None` = every universe).
    pub universes: Option<BTreeSet<String>>,
}

/// `EntitlementsOut`: docs/07's seven keys, in its order.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct EntitlementsOut {
    /// `screener`.
    pub screener: bool,
    /// `export_csv`.
    pub export_csv: bool,
    /// `custom_columns`.
    pub custom_columns: bool,
    /// `historical_ranks`.
    pub historical_ranks: bool,
    /// `backtests`.
    pub backtests: bool,
    /// `api_access`.
    pub api_access: bool,
    /// `max_screens`.
    pub max_screens: i64,
}

impl Entitlements {
    fn with(granted: &[&'static str], max_screens: i64) -> Self {
        Self {
            granted: granted.iter().copied().collect(),
            max_screens,
            universes: None,
        }
    }

    /// `ANONYMOUS`: the screener, nothing saved.
    #[must_use]
    pub fn anonymous() -> Self {
        Self::with(&["screener"], 0)
    }

    /// `REGISTERED`: the screener and five saved screens.
    #[must_use]
    pub fn registered() -> Self {
        Self::with(&["screener"], FREE_MAX_SCREENS)
    }

    /// `FREE_TIER`: `REGISTERED` restricted to NIFTY 50.
    #[must_use]
    pub fn free_tier() -> Self {
        let mut e = Self::registered();
        e.universes = Some(std::iter::once("nifty-50".to_owned()).collect());
        e
    }

    /// `allows(feature)`.
    #[must_use]
    pub fn allows(&self, feature: &str) -> bool {
        self.granted.contains(feature)
    }

    /// `as_dict()` as the response model.
    #[must_use]
    pub fn as_out(&self) -> EntitlementsOut {
        EntitlementsOut {
            screener: self.allows("screener"),
            export_csv: self.allows("export_csv"),
            custom_columns: self.allows("custom_columns"),
            historical_ranks: self.allows("historical_ranks"),
            backtests: self.allows("backtests"),
            api_access: self.allows("api_access"),
            max_screens: self.max_screens,
        }
    }

    /// `from_plan_features(features)`: a missing key refuses, an unknown key is ignored.
    #[must_use]
    pub fn from_plan_features(features: &OrderedJson) -> Self {
        let empty = OrderedJson::Object(Vec::new());
        let block = match features.get(ENTITLEMENTS_KEY) {
            Some(b @ OrderedJson::Object(_)) => b,
            _ => &empty,
        };
        let granted = FEATURES
            .iter()
            .copied()
            .filter(|f| block.get(f) == Some(&OrderedJson::Bool(true)))
            .collect();
        let max_screens = match block.get(MAX_SCREENS_KEY) {
            Some(OrderedJson::Int(i)) => *i,
            _ => 0,
        };
        let universes = match block.get(UNIVERSES_KEY) {
            Some(OrderedJson::Array(items)) if !items.is_empty() => Some(
                items
                    .iter()
                    .map(|v| match v {
                        OrderedJson::Str(s) => s.clone(),
                        other => other.to_python_json().unwrap_or_default(),
                    })
                    .collect(),
            ),
            _ => None,
        };
        Self {
            granted,
            max_screens,
            universes,
        }
    }
}

/// `Override`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Override {
    /// `feature`.
    pub feature: String,
    /// `grant` (`effect == "grant"`).
    pub grant: bool,
    /// `value` (only for `max_screens`).
    pub value: Option<i64>,
}

/// `apply_overrides(base, overrides)`.
#[must_use]
pub fn apply_overrides(base: &Entitlements, overrides: &[Override]) -> Entitlements {
    let mut granted = base.granted.clone();
    let mut max_screens = base.max_screens;
    for o in overrides {
        if o.feature == MAX_SCREENS_KEY {
            if o.grant
                && let Some(v) = o.value
            {
                max_screens = v;
            }
            continue;
        }
        let Some(feature) = FEATURES.iter().copied().find(|f| *f == o.feature) else {
            continue;
        };
        if o.grant {
            granted.insert(feature);
        } else {
            granted.remove(feature);
        }
    }
    Entitlements {
        granted,
        max_screens,
        universes: base.universes.clone(),
    }
}

/// `datetime.now(tz=UTC)`.
#[must_use]
pub fn utc_now() -> DateTime<Utc> {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default();
    DateTime::from_timestamp(
        i64::try_from(now.as_secs()).unwrap_or(0),
        now.subsec_nanos(),
    )
    .unwrap_or_default()
}

/// `entitlements_for(session, principal, settings=)`.
///
/// # Errors
/// A database failure, as the 500 catch-all.
pub async fn entitlements_for(
    db: &Db,
    principal: &Principal,
    settings: &Settings,
) -> Result<Entitlements, Problem> {
    let Some(user_id) = principal.user_id else {
        return Ok(Entitlements::anonymous());
    };
    let moment = utc_now();
    // resolve_active_grant: newest entitling subscription whose period has not ended.
    let rows = sqlx::query(
        "SELECT s.current_period_end, p.features FROM subscription s \
         JOIN plan p ON p.id = s.plan_id \
         WHERE s.user_id = $1 AND s.status = ANY($2) \
         ORDER BY s.started_at DESC, s.id DESC",
    )
    .bind(user_id)
    .bind(ENTITLING_STATUSES.as_slice())
    .fetch_all(db.pool())
    .await
    .map_err(|e| crate::db_failure(&e.into()))?;
    let mut base: Option<Entitlements> = None;
    for r in &rows {
        let end =
            row::opt_timestamptz(r, "current_period_end").map_err(|e| crate::db_failure(&e))?;
        if end.is_none_or(|end| end > moment) {
            let features = row::jsonb(r, "features").map_err(|e| crate::db_failure(&e))?;
            base = Some(Entitlements::from_plan_features(&features));
            break;
        }
    }
    let base = base.unwrap_or_else(|| {
        if settings.free_tier_enabled {
            Entitlements::free_tier()
        } else {
            Entitlements::registered()
        }
    });
    // active_overrides: expiry filtered in code, one rule (`EntitlementOverride.is_active`).
    let rows = sqlx::query(
        "SELECT feature, effect, value, expires_at FROM entitlement_override WHERE user_id = $1",
    )
    .bind(user_id)
    .fetch_all(db.pool())
    .await
    .map_err(|e| crate::db_failure(&e.into()))?;
    let mut overrides = Vec::new();
    for r in &rows {
        let expires = row::opt_timestamptz(r, "expires_at").map_err(|e| crate::db_failure(&e))?;
        if expires.is_some_and(|x| x <= moment) {
            continue;
        }
        let feature: String = r
            .try_get("feature")
            .map_err(|e| crate::db_failure(&e.into()))?;
        let effect: String = r
            .try_get("effect")
            .map_err(|e| crate::db_failure(&e.into()))?;
        let value: Option<i64> = r
            .try_get("value")
            .map_err(|e| crate::db_failure(&e.into()))?;
        overrides.push(Override {
            feature,
            grant: effect == "grant",
            value,
        });
    }
    Ok(if overrides.is_empty() {
        base
    } else {
        apply_overrides(&base, &overrides)
    })
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    #[test]
    fn the_shape_is_docs_07s_seven_keys_in_order() {
        let body = crate::wire::to_json_bytes(&Entitlements::registered().as_out());
        assert_eq!(
            String::from_utf8(body).unwrap(),
            r#"{"screener":true,"export_csv":false,"custom_columns":false,"historical_ranks":false,"backtests":false,"api_access":false,"max_screens":5}"#
        );
    }

    #[test]
    fn a_plan_row_grants_only_what_is_true_and_a_typo_grants_nothing() {
        let features = OrderedJson::parse(
            r#"{"entitlements": {"screener": true, "backtests": 1, "export_csv": "yes",
                "max_screens": true, "api_acess": true}}"#,
        )
        .unwrap();
        let e = Entitlements::from_plan_features(&features);
        assert_eq!(e.granted, ["screener"].into_iter().collect());
        assert_eq!(e.max_screens, 0, "a bool is not a max_screens");
    }

    #[test]
    fn overrides_layer_on_top_and_an_unknown_feature_is_ignored() {
        let base = Entitlements::registered();
        let out = apply_overrides(
            &base,
            &[
                Override {
                    feature: "backtests".into(),
                    grant: true,
                    value: None,
                },
                Override {
                    feature: "screener".into(),
                    grant: false,
                    value: None,
                },
                Override {
                    feature: "max_screens".into(),
                    grant: true,
                    value: Some(50),
                },
                Override {
                    feature: "max_screens".into(),
                    grant: true,
                    value: None,
                },
                Override {
                    feature: "slack".into(),
                    grant: true,
                    value: None,
                },
            ],
        );
        assert_eq!(out.granted, ["backtests"].into_iter().collect());
        assert_eq!(out.max_screens, 50);
    }
}
