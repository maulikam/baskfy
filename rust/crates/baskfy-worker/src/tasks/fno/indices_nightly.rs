//! The index service's evening increment (`baskfy_api::indices`): master join, Kite bars, stats,
//! and constituent lists older than a week. One transaction per step; each step's failure is
//! reported, never fatal to the next. Reads Kite only.

use baskfy_api::indices::{history, master, nse};
use baskfy_db::WriteDb;
use baskfy_providers::kite::KiteProvider;
use serde_json::{Map, Value, json};

async fn step<T, F>(db: &WriteDb, out: &mut Map<String, Value>, key: &str, body: F) -> Option<T>
where
    F: for<'c> FnOnce(
        &'c mut sqlx::PgConnection,
    ) -> std::pin::Pin<Box<dyn Future<Output = Result<T, String>> + Send + 'c>>,
{
    let res = async {
        let mut tx = db.begin().await.map_err(|e| e.to_string())?;
        let v = body(&mut tx).await?;
        tx.commit().await.map_err(|e| e.to_string())?;
        Ok::<T, String>(v)
    }
    .await;
    match res {
        Ok(v) => Some(v),
        Err(e) => {
            out.insert(format!("{key}_error"), json!(e));
            None
        }
    }
}

/// Run the increment; the report is logged by the caller.
pub(crate) async fn run(db: &WriteDb, kite: Option<&KiteProvider>) -> Value {
    let mut out = Map::new();
    let kite_rows = match kite {
        Some(k) => master::kite_index_instruments(k).await.unwrap_or_else(|e| {
            out.insert("kite_instruments_error".into(), json!(e));
            Vec::new()
        }),
        None => Vec::new(),
    };
    if let Some(n) = step(db, &mut out, "master", move |c| {
        Box::pin(async move { master::refresh_master(c, &kite_rows).await })
    })
    .await
    {
        out.insert("master".into(), json!(n));
    }
    let today = baskfy_api::indices::now_ist().date_naive();
    if let Some(k) = kite {
        let plan = step(db, &mut out, "history_plan", move |c| {
            Box::pin(history::plan(c, today, None))
        })
        .await
        .unwrap_or_default();
        let (mut ok, mut failed) = (0_i64, Vec::new());
        for (id, token, start, end) in plan {
            match history::fetch_bars(k, token, start, end).await {
                Ok(bars) => {
                    let wrote = step(db, &mut out, "history_write", move |c| {
                        Box::pin(async move { history::write_bars(c, id, &bars).await })
                    })
                    .await;
                    if wrote.is_some() {
                        ok += 1;
                    } else {
                        failed.push(id);
                    }
                }
                Err(_) => failed.push(id),
            }
        }
        out.insert("history".into(), json!({"ok": ok, "failed": failed}));
    }
    if let Some(n) = step(db, &mut out, "stats", |c| {
        Box::pin(history::recompute_all_stats(c))
    })
    .await
    {
        out.insert("stats_rows".into(), json!(n));
    }
    let due = step(db, &mut out, "constituents_due", |c| Box::pin(nse::due(c, false)))
        .await
        .unwrap_or_default();
    let (got, failed) = nse::fetch_due(&due).await;
    let mut ok = 0_i64;
    for (id, stem, rows) in got {
        if step(db, &mut out, "constituents_write", move |c| {
            Box::pin(async move { nse::write(c, id, &stem, &rows).await })
        })
        .await
        .is_some()
        {
            ok += 1;
        }
    }
    out.insert(
        "constituents".into(),
        json!({"ok": ok, "failed": failed}),
    );
    Value::Object(out)
}
