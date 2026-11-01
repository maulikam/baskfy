//! The worker's daemons: Celery beat (`beat --serve`) and Celery workers (`serve --queues`).

pub mod beat_daemon;
pub mod celery;
pub mod consumer;

use std::future::Future;

tokio::task_local! {
    static TASK_ID: String;
}

/// The id of the Celery message the running task was delivered by (`self.request.id`).
#[must_use]
pub fn current_task_id() -> Option<String> {
    TASK_ID.try_with(Clone::clone).ok()
}

/// Run `fut` as the task delivered with `task_id`.
pub async fn with_task_id<F: Future>(task_id: String, fut: F) -> F::Output {
    TASK_ID.scope(task_id, fut).await
}

/// Resolves on the first SIGTERM or SIGINT; a second one sends `true` on `cold`.
///
/// # Errors
/// The signal handlers could not be installed.
pub fn shutdown_signals(
    cold: tokio::sync::watch::Sender<bool>,
) -> std::io::Result<impl Future<Output = ()>> {
    use tokio::signal::unix::{SignalKind, signal};
    let mut term = signal(SignalKind::terminate())?;
    let mut int = signal(SignalKind::interrupt())?;
    let (warm_tx, mut warm_rx) = tokio::sync::watch::channel(false);
    tokio::spawn(async move {
        let mut seen = 0_u32;
        loop {
            tokio::select! {
                _ = term.recv() => {}
                _ = int.recv() => {}
            }
            seen += 1;
            if seen == 1 {
                let _ = warm_tx.send(true);
            } else {
                let _ = cold.send(true);
            }
        }
    });
    Ok(async move {
        let _ = warm_rx.wait_for(|v| *v).await;
    })
}

pub(crate) fn log(level: &str, message: &str) {
    use std::io::Write as _;
    let _ = writeln!(std::io::stderr(), "{level} baskfy_worker.runtime: {message}");
}
