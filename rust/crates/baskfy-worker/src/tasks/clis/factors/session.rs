//! `run_checkpointed`'s session: one connection whose `checkpoint` commits what is done so far.

use baskfy_db::WriteDb;
use sqlx::{PgConnection, Postgres, Transaction};

use crate::error::WorkerError;

/// A session a long job writes through, checkpointing as it goes.
pub(crate) trait Session {
    /// The connection the next statement runs on.
    fn conn(&mut self) -> &mut PgConnection;
    /// Makes what is written so far durable (`session.commit`), or nothing in a test (`flush`).
    fn checkpoint(&mut self) -> impl Future<Output = Result<(), WorkerError>>;
}

/// The CLI's session: a transaction committed at every checkpoint and at the end.
pub(crate) struct Checkpointed {
    db: WriteDb,
    tx: Transaction<'static, Postgres>,
}

impl Checkpointed {
    pub(crate) async fn open(db: &WriteDb) -> Result<Self, WorkerError> {
        let tx = db.begin().await?;
        Ok(Self { db: db.clone(), tx })
    }

    pub(crate) async fn finish(self) -> Result<(), WorkerError> {
        self.tx.commit().await?;
        Ok(())
    }
}

impl Session for Checkpointed {
    fn conn(&mut self) -> &mut PgConnection {
        &mut self.tx
    }

    async fn checkpoint(&mut self) -> Result<(), WorkerError> {
        let next = self.db.begin().await?;
        let done = std::mem::replace(&mut self.tx, next);
        done.commit().await?;
        Ok(())
    }
}

/// A test's session: everything inside the caller's transaction; a checkpoint commits nothing.
#[cfg(test)]
pub(crate) struct Flat<'a>(pub &'a mut PgConnection);

#[cfg(test)]
impl Session for Flat<'_> {
    fn conn(&mut self) -> &mut PgConnection {
        self.0
    }

    async fn checkpoint(&mut self) -> Result<(), WorkerError> {
        Ok(())
    }
}
