//! `baskfy-worker migrate status|up`: the schema runner that replaced Alembic. The SQL is read from
//! files in the image, so a SQL-only change reuses the compiled binary.

use std::path::{Path, PathBuf};
use std::process::ExitCode;

use baskfy_db::WriteDb;
use sqlx::migrate::{Migrate as _, Migrator};
use sqlx::{PgPool, Postgres, pool::PoolConnection};

const FROZEN_ALEMBIC: &str = "0063_mtf_leverage";
const BASELINE_VERSION: i64 = 1;
const DEFAULT_DIR: &str = "/usr/share/baskfy/migrations";
const LOCK_KEY: i64 = 0x0BA5_C1FB;
const STATUS_PENDING: u8 = 3;

type Failure = String;

fn directory() -> PathBuf {
    std::env::var_os("BASKFY_MIGRATIONS_DIR")
        .map_or_else(|| PathBuf::from(DEFAULT_DIR), PathBuf::from)
}

async fn exists(pool: &PgPool, table: &str) -> Result<bool, Failure> {
    sqlx::query_scalar::<_, bool>("SELECT to_regclass($1) IS NOT NULL")
        .bind(format!("public.{table}"))
        .fetch_one(pool)
        .await
        .map_err(|e| e.to_string())
}

async fn applied(pool: &PgPool) -> Result<Vec<i64>, Failure> {
    if !exists(pool, "_sqlx_migrations").await? {
        return Ok(Vec::new());
    }
    sqlx::query_scalar("SELECT version FROM _sqlx_migrations WHERE success ORDER BY version")
        .fetch_all(pool)
        .await
        .map_err(|e| e.to_string())
}

/// Lines the database has that the baseline does not, and the reverse.
async fn drift(pool: &PgPool, dir: &Path) -> Result<Vec<String>, Failure> {
    let read =
        |name: &str| std::fs::read_to_string(dir.join(name)).map_err(|e| format!("{name}: {e}"));
    let sql = read("fingerprint.sql")?;
    let expected = read("baseline.schema.txt")?;
    let actual: Vec<String> = sqlx::query_scalar(sql.as_str())
        .fetch_all(pool)
        .await
        .map_err(|e| e.to_string())?;
    let want: std::collections::BTreeSet<&str> = expected.lines().collect();
    let have: std::collections::BTreeSet<&str> = actual.iter().map(String::as_str).collect();
    let mut out: Vec<String> = have.difference(&want).map(|l| format!("+ {l}")).collect();
    out.extend(want.difference(&have).map(|l| format!("- {l}")));
    Ok(out)
}

/// An Alembic database joins at the baseline, only if its schema really is the baseline.
async fn adopt(
    pool: &PgPool,
    conn: &mut PoolConnection<Postgres>,
    migrator: &Migrator,
    dir: &Path,
) -> Result<(), Failure> {
    let version: String = sqlx::query_scalar("SELECT version_num FROM alembic_version")
        .fetch_one(pool)
        .await
        .map_err(|e| e.to_string())?;
    if version != FROZEN_ALEMBIC {
        return Err(format!(
            "this database is at Alembic {version}; only {FROZEN_ALEMBIC} can adopt the baseline"
        ));
    }
    let differences = drift(pool, dir).await?;
    if !differences.is_empty() {
        let shown = differences
            .iter()
            .take(20)
            .cloned()
            .collect::<Vec<_>>()
            .join("\n");
        return Err(format!(
            "the schema differs from the baseline in {} place(s); nothing was changed:\n{shown}",
            differences.len()
        ));
    }
    let baseline = migrator
        .iter()
        .find(|m| m.version == BASELINE_VERSION)
        .ok_or_else(|| "the baseline migration is missing".to_owned())?;
    conn.ensure_migrations_table()
        .await
        .map_err(|e| e.to_string())?;
    sqlx::query(
        "INSERT INTO _sqlx_migrations (version, description, success, checksum, execution_time) \
         VALUES ($1, $2, TRUE, $3, 0)",
    )
    .bind(baseline.version)
    .bind(baseline.description.as_ref())
    .bind(baseline.checksum.as_ref())
    .execute(&mut **conn)
    .await
    .map_err(|e| e.to_string())?;
    println!("adopted: Alembic {FROZEN_ALEMBIC} recorded as baseline {BASELINE_VERSION}");
    Ok(())
}

async fn up(pool: &PgPool, migrator: &Migrator, dir: &Path) -> Result<(), Failure> {
    let mut conn = pool.acquire().await.map_err(|e| e.to_string())?;
    sqlx::query("SELECT pg_advisory_lock($1)")
        .bind(LOCK_KEY)
        .execute(&mut *conn)
        .await
        .map_err(|e| e.to_string())?;
    if !exists(pool, "_sqlx_migrations").await? {
        if exists(pool, "alembic_version").await? {
            adopt(pool, &mut conn, migrator, dir).await?;
        } else {
            let tables: i64 =
                sqlx::query_scalar("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")
                    .fetch_one(pool)
                    .await
                    .map_err(|e| e.to_string())?;
            if tables > 0 {
                return Err(format!(
                    "public has {tables} table(s) but no migration history; refusing to guess"
                ));
            }
        }
    }
    migrator.run(&mut *conn).await.map_err(|e| e.to_string())?;
    sqlx::query("SELECT pg_advisory_unlock($1)")
        .bind(LOCK_KEY)
        .execute(&mut *conn)
        .await
        .map_err(|e| e.to_string())?;
    Ok(())
}

async fn status(pool: &PgPool, migrator: &Migrator) -> Result<ExitCode, Failure> {
    let done = applied(pool).await?;
    let pending: Vec<i64> = migrator
        .iter()
        .map(|m| m.version)
        .filter(|v| !done.contains(v))
        .collect();
    println!("applied: {done:?}\npending: {pending:?}");
    Ok(if pending.is_empty() {
        ExitCode::SUCCESS
    } else {
        ExitCode::from(STATUS_PENDING)
    })
}

/// # Exit codes
/// 0 current, 1 failed or refused, 2 usage, 3 `status` found pending migrations.
#[allow(clippy::print_stdout, clippy::print_stderr)]
pub async fn run(db: &WriteDb, rest: &[String]) -> ExitCode {
    let dir = directory();
    let migrator = match Migrator::new(dir.as_path()).await {
        Ok(m) => m,
        Err(e) => {
            eprintln!("migrate: {}: {e}", dir.display());
            return ExitCode::FAILURE;
        }
    };
    let pool = db.pool();
    let outcome = match rest.first().map(String::as_str) {
        Some("status") => status(pool, &migrator).await,
        Some("up") => match up(pool, &migrator, &dir).await {
            Ok(()) => status(pool, &migrator).await,
            Err(e) => Err(e),
        },
        _ => {
            eprintln!("usage: baskfy-worker migrate status|up");
            return ExitCode::from(2);
        }
    };
    match outcome {
        Ok(code) => code,
        Err(e) => {
            eprintln!("migrate: {e}");
            ExitCode::FAILURE
        }
    }
}
