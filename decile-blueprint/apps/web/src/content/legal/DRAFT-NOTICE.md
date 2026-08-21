# ⚠️ DRAFTS REQUIRING LEGAL REVIEW

Every `.mdx` file in this directory is a **draft written by engineers, not by lawyers**, and every
one of them opens with the same notice in an MDX comment.

PROMPTS.md Prompt 18 deliverable 3:

> Draft them specifically for this product (Indian jurisdiction, non-SEBI-registered, prepaid
> lifetime plan treatment, data licensing constraints). Mark them clearly as **DRAFTS REQUIRING
> LEGAL REVIEW** at the top of each file in the repo, **not on the rendered page**.

The marker is therefore in an MDX comment (`{/* … */}`), which compiles to nothing. It is asserted
by `apps/web/src/lib/__tests__/legal-drafts.test.ts`, which also asserts that the marker text does
**not** appear in the rendered output — a "DRAFT" watermark on a live terms page would be worse
than no terms page, because it invites a customer to argue that nothing was agreed.

## What a reviewer must decide before the first real charge

1. **The supplier's legal identity, address, and GSTIN.** Every document says
   `[SUPPLIER LEGAL NAME]`, `[SUPPLIER ADDRESS]` and `[SUPPLIER GSTIN]` and none of them is filled
   in, because none of them is known to this repository. A tax invoice without a real GSTIN is not
   a tax invoice.
2. **The GST rate and the SAC code.** 18% and SAC 998439 are defaults taken from the general rate
   for information-technology services. They are settings (`BASKFY_GST_RATE_PERCENT`,
   `BASKFY_GST_SAC_CODE`) and every issued invoice records what it was raised at, so a correction
   cannot rewrite history — but nobody qualified has confirmed either.
3. **Whether the prices are GST-inclusive.** They are, in this build: the advertised figure is
   charged and the tax is back-computed. The alternative reading of docs/01 §1 is that ₹500 is the
   taxable value and ₹590 is charged. This is a commercial decision with tax consequences.
4. **The "Forever" plan's accounting and consumer-law treatment.** docs/11 requires prepaid
   lifetime revenue to be treated as deferred revenue. The consumer-protection question — what a
   buyer is owed if the service closes in year two — is not settled by that and is not settled
   here.
5. **The refund terms.** The drafted policy is what the code actually does today, which is: no
   automated refunds, `refund.*` gateway events acknowledged and ignored, refunds handled by hand
   at our discretion. A reviewer may conclude that Indian consumer law requires more.
6. **The data-licensing paragraphs.** Market data reaches us under licences for our own use.
   The drafts say we publish derived analytics and do not redistribute vendor bars. A written
   data-redistribution opinion is still outstanding (docs/11 §"Data licensing") and the public API
   stays switched off until it exists.
7. **The DPDP Act specifics.** Consent, notice, grievance officer, breach notification and
   cross-border transfer are all touched on. The Act's rules were still being notified in stages
   at the time of writing and the drafts do not name a Data Protection Officer or a grievance
   officer, because no such person has been appointed.
8. **The governing law and venue clause.** The drafts name Indian law and a placeholder city.
   Choose one that is actually where the supplier is.

Nothing in this directory has been read by a lawyer. Do not launch on it.
