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

1. **The supplier's legal identity, address, and GSTIN.** ✅ Supplied 27 Aug 2026 and filled in. RENIL, a sole proprietorship at 703/2, Sector 4C, Gandhinagar, Gujarat 382006,
   GSTIN `24ANFPD9399F1ZS`. Consistent on inspection: state code 24 is Gujarat, and the embedded
   PAN's fourth character `P` is the individual-proprietor code. A reviewer should still confirm
   the registration is live and that the trade name on the GST certificate matches what the
   documents call the supplier.
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
   at the time of writing. A **grievance officer is now named** — Maulik,
   `grievance@baskfy.com` (27 Aug 2026). Two things about that still need a decision:
   the Consumer Protection (E-Commerce) Rules contemplate a full legal name, and only a first
   name is recorded; and **`grievance@baskfy.com` has no inbound mail route** — SES on this
   deployment is send-only, so mail to that address currently goes nowhere. A grievance address
   that bounces is worse than none. No Data Protection Officer is named, and whether one is
   required has not been assessed.
8. **The governing law and venue clause.** ✅ Settled 27 Aug 2026: Indian law, courts at
   Gandhinagar — which is where the supplier is registered, as this item required.

## Two values filled with defaults on 27 Aug 2026, and one of them is not yet enforced

Maulik asked for the conventional defaults rather than leaving these open.

**Server log retention — 90 days.** That is the common default and it is now what
`privacy-policy.mdx` §5 states. **The box does not enforce it.** Container logs rotate on the
Docker `json-file` driver at `max-size=50m, max-file=5` — 250 MB per service, discarded by
*size*, not by age. At present traffic that is very likely more than ninety days of logs, so the
policy currently promises a shorter retention than the infrastructure delivers. Closing this is a
cron or a log shipper with a time-based lifecycle, and until it exists this is the one sentence
in these documents that describes an intention rather than a behaviour. It is written down here
rather than softened in the policy, because "90 days" is the right commitment and the fix belongs
on the box.

**Cross-border transfers — DPDP §16, and the countries named.** Section 16 permits transfer to
any country the Central Government has not restricted by notification, which is a negative-list
regime rather than an adequacy regime. §4 now names where data actually goes: India for AWS
`ap-south-1`, Razorpay and SES; the United States for Google's authentication and for Sentry when
it is enabled; Cloudflare R2 possibly outside India. A reviewer should confirm the notified
restriction list is still empty of these at the time of launch.

## What M46 changed here

Google sign-in replaced registration, the password and the email OTP
(`docs/DECISIONS-MERGE.md` M46). `privacy-policy.mdx` §1, §2, §3, §4, §6 and §7 were rewritten to
match: there is no password to hash, no sign-in code to mail, no lockout counter to justify, and
**Google is a new processor** that receives an authentication request for every sign-in and is the
source of the email address. A privacy policy that described data we no longer collect and omitted
a processor we now use would be a DPDP problem in itself, so this is a correction of fact rather
than a drafting preference — but a reviewer should still read §4's Google entry and §7 against
what they know of the Act.

Nothing in this directory has been read by a lawyer. Do not launch on it.
