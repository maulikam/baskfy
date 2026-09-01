# Baskfy — brief for counsel

**One document to send to a lawyer.** Everything that needs a legal opinion is consolidated here
with a pointer to the source. It exists because the questions were spread across three places —
`apps/web/src/content/legal/DRAFT-NOTICE.md`, `NEEDS-MAULIK.md` §13 (C1–C3), and
`docs/DECISIONS-MERGE.md` §D7/§D10 — and nobody engaging a lawyer should have to assemble them.

**Written by an engineer, not a lawyer.** Nothing here is a legal conclusion. It is a description
of what the software does today and a list of the decisions that require someone qualified.

Compiled 25 Aug 2026. Jurisdiction: India. Verify the state of play before sending — the facts
below were measured on that date and the code moves.

---

## 1. What Baskfy is, in one paragraph

An India-equities momentum screener and curated-basket product. It publishes ranked baskets and
order *plans*; **it never places an order from the web app** — a user executes at their own broker.
A separate single-operator desk (`kite-momentum-rebalancer`) does place orders, in its owner's own
account, and only on explicit confirmation. Regulatory posture is recorded as **"B"** in
`docs/DECISIONS-MERGE.md` §D3, written 23 Aug 2026 and marked ⚠ UNREVIEWED — that posture is
itself one of the things counsel is being asked to confirm.

## 2. The most urgent item — live pages showing unfilled placeholders

**Four legal pages are publicly reachable right now and render bracketed placeholders to any
visitor.** Measured 25 Aug 2026 against the running app (all four return HTTP 200):

| Page | Placeholders a visitor currently sees |
|---|---|
| `/terms-conditions` | `[SUPPLIER LEGAL NAME]` `[SUPPLIER ENTITY TYPE]` `[SUPPLIER ADDRESS]` `[SUPPLIER GSTIN]` `[GRIEVANCE OFFICER NAME]` `[GRIEVANCE OFFICER EMAIL]` `[CITY]` |
| `/privacy-policy` | `[SUPPLIER LEGAL NAME]` `[SUPPLIER ADDRESS]` `[GRIEVANCE OFFICER NAME]` `[GRIEVANCE OFFICER EMAIL]` `[HOSTING PROVIDER]` `[EMAIL PROVIDER]` |
| `/disclaimer` | `[SUPPLIER LEGAL NAME]` |
| `/refund-policy` | `[GRIEVANCE OFFICER NAME]` `[GRIEVANCE OFFICER EMAIL]` |

These are **not** a rendering bug — the values are genuinely unknown to the repository. They need
facts only the business can supply, and they are listed in §6 as a checklist.

The repo-side draft marker is deliberately invisible on the rendered page (Prompt 18: mark drafts
"in the repo, **not on the rendered page**", on the reasoning that a "DRAFT" watermark on a live
terms page invites a customer to argue nothing was agreed). That decision is worth counsel's view
too, given the pages are already public.

## 3. The four documents

All under `decile-blueprint/apps/web/src/content/legal/`. None has been read by a lawyer.

| Document | Size | Renders at |
|---|---|---|
| `terms-conditions.mdx` | 1,198 words | `/terms-conditions` |
| `privacy-policy.mdx` | 985 words | `/privacy-policy` |
| `disclaimer.mdx` | 806 words | `/disclaimer` |
| `refund-policy.mdx` | 752 words | `/refund-policy` |

## 4. The eight questions on the documents

Verbatim in substance from `DRAFT-NOTICE.md`, which is the authoritative version:

1. **Supplier legal identity, address and GSTIN.** All placeholders. *A tax invoice without a real
   GSTIN is not a tax invoice.*
2. **GST rate and SAC code.** 18% and SAC 998439 are engineering defaults for IT services, held in
   `BASKFY_GST_RATE_PERCENT` / `BASKFY_GST_SAC_CODE`. Every issued invoice records the rate it was
   raised at, so a correction cannot rewrite history. Nobody qualified has confirmed either.
3. **Are prices GST-inclusive?** They are in this build — the advertised figure is charged and tax
   is back-computed. The alternative reading is that ₹500 is the taxable value and ₹590 is charged.
   Commercial decision with tax consequences.
4. **The "Forever" plan.** `docs/11` requires prepaid lifetime revenue to be treated as deferred
   revenue. What a buyer is owed if the service closes in year two is not settled by that.
5. **Refund terms.** The drafted policy describes what the code does: no automated refunds,
   `refund.*` gateway events acknowledged and ignored, refunds by hand at discretion. Counsel may
   conclude Indian consumer law requires more.
6. **Data-licensing paragraphs.** Market data reaches us under licences for our own use. The drafts
   say we publish derived analytics and do not redistribute vendor bars. A written
   data-redistribution opinion is outstanding; **the public API stays switched off until it
   exists** (enforced in code — see §5).
7. **DPDP Act specifics.** Consent, notice, grievance officer, breach notification and
   cross-border transfer are all touched on. No Data Protection Officer or grievance officer has
   been appointed, which is why those placeholders are live on the site today.
8. **Governing law and venue.** Indian law, placeholder city. Choose where the supplier actually is.

## 5. The three regulatory questions (C1–C3)

From `NEEDS-MAULIK.md` §13. None currently blocks engineering; C3 blocks paid multi-tenant launch.

| # | Question | Why it matters |
|---|---|---|
| **C1** | **Algo ID / exchange registration** when Baskfy supplies order *plans* to a third-party Kite app, versus a personal algo in one's own account at under 10 orders/sec | Per-order tagging and broker empanelment, if this lands on the "algo supplied to others" side |
| **C2** | **Research versus advice** for ranked baskets — and what changes when a basket is personalised to a user's existing holdings | Marketing claims, disclaimer wording, RA paperwork |
| **C3** | Whether posture B needs **RA registration** and/or **Kite-Publisher / empanelment** | Product claims and the SEBI filing path. **Yes before multi-tenant paid**; no for a sole-tenant operator desk |

Two further items are recorded as ⚠ UNREVIEWED stubs and need a commercial decision before they
need a legal one: **D7** (pricing amounts) and **D10** (market-data display licensing).

## 6. Checklist — facts only the business can supply

Not legal questions. Nothing can be filled in until these exist, and the live pages show
placeholders until they do.

- [ ] Supplier legal name, and entity type (proprietorship / LLP / private limited)
- [ ] Registered address
- [ ] GSTIN
- [ ] Grievance officer: name and email (**required by the DPDP Act**)
- [ ] Governing-law city / venue
- [ ] Hosting provider and email provider, named for the privacy policy
- [ ] Confirmation that prices are GST-inclusive, or a decision to change it

## 7. What the code already enforces, so counsel knows what is *not* at risk

Worth stating, because it narrows the questions:

- **The public API is switched off** behind a source constant plus a flag (decision D9), and stays
  off until the data-redistribution opinion exists.
- **The web app has no execute route.** Non-negotiable #1 of the desk's charter: orders fire only
  from an explicit confirmed plan, never from the web.
- **Track B flags stay false** pending D7/D10.
- **Every invoice records the tax rate it was raised at**, so a later correction to the rate cannot
  silently rewrite past invoices.
- **The draft marker is asserted to be repo-only** by `src/lib/__tests__/legal-drafts.test.ts`
  (22 tests, passing), which checks both that the marker is present in every source file and that
  it does not reach the rendered page.

## 8. Sources

- `decile-blueprint/apps/web/src/content/legal/DRAFT-NOTICE.md` — the eight questions, authoritative
- `NEEDS-MAULIK.md` §13 — C1–C3, and the D3 posture history
- `docs/DECISIONS-MERGE.md` §D3 (posture B, ⚠ UNREVIEWED), §D7, §D10
- `decile-blueprint/docs/11` — data licensing, deferred revenue
- `decile-blueprint/apps/web/src/lib/marketing/routes.ts` — where the four pages are published
