//! "Did NSE publish a bhavcopy for this date?" — the one question that separates a holiday from
//! a failed ingest. Ports `baskfy_providers.publication`.
//!
//! The probe lives here, below both the worker and the API, so both ask the same question
//! (`reconcile_calendar` and the resync detector) and get one answer.
//!
//! **The answer is deliberately asymmetric.** `false` means "we found no published file", which
//! covers both "NSE published nothing" and "we could not tell". That is the safe direction for
//! the calendar, which only consults it to *refuse* to infer a holiday. A caller that acts on a
//! `true` must treat `false` as "no evidence", never as "the exchange was shut".

use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;

use chrono::NaiveDate;

use crate::ports::ReferenceProvider;

/// `PublicationCheck`: "did NSE publish a bhavcopy for this date?", answered asynchronously.
pub type PublicationCheck =
    Arc<dyn Fn(NaiveDate) -> Pin<Box<dyn Future<Output = bool> + Send>> + Send + Sync>;

/// `bhavcopy_publication_check`: wrap a provider's bhavcopy lookup as the question.
///
/// `None` in, `None` out — Python returns `None` for a provider with no `bhavcopy`, which
/// leaves `reconcile_calendar` on its pre-M62 behaviour rather than treating "cannot check" as
/// "nothing was published". In Rust every [`ReferenceProvider`] has a bhavcopy, so the caller
/// passes `None` exactly when it holds no reference provider.
///
/// A provider error (no file; the archive unreadable) answers `false`; a frame answers whether
/// it has rows.
#[must_use]
pub fn bhavcopy_publication_check(
    provider: Option<Arc<dyn ReferenceProvider>>,
) -> Option<PublicationCheck> {
    let provider = provider?;
    let check: PublicationCheck = Arc::new(move |day: NaiveDate| {
        let provider = Arc::clone(&provider);
        Box::pin(async move {
            match provider.bhavcopy(day).await {
                Ok(frame) => frame.height() > 0,
                Err(_) => false,
            }
        })
    });
    Some(check)
}
