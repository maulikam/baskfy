# 11 — Non-functional requirements

## Performance budgets

| Surface | Target |
|---|---|
| Screen run, warm cache | p95 < 150 ms |
| Screen run, cold | p95 < 800 ms |
| Instrument factsheet (RSC) | TTFB < 300 ms, LCP < 1.8 s |
| Dashboard (145 indices) | < 500 ms |
| Backtest (15y, monthly, 20 names) | < 10 s |
| Nightly pipeline end-to-end | < 45 min |
| CSV export (4,000 rows) | < 2 s |

Client: JS on the screens route < 250 KB gzip after code-splitting the table and charts.

## Security

- Argon2id password hashing; OTP login as the default path, password optional.
- JWT: HS256, 15-min access, rotating refresh in an httpOnly, `SameSite=Lax`, Secure cookie.
- CSRF protection on all cookie-authenticated mutations.
- Strict CSP (`default-src 'self'`), HSTS, `X-Content-Type-Options`, `Referrer-Policy`.
- All SQL parameterised. The factor registry is a whitelist — **no user string ever reaches a
  SQL expression**.
- Rate limiting per IP and per user; exponential backoff on auth endpoints; account lockout
  after 10 failures with email notification.
- Razorpay webhooks: verify signature, dedupe by event id, process idempotently.
- Secrets in the platform's secret store, never in the repo. Kite access token encrypted at rest.
- Dependency scanning (Dependabot + `pip-audit` + `pnpm audit`) in CI; block on high severity.
- PII inventory: email, name, payment metadata. No trading credentials are ever collected —
  the app never asks for a broker login from end users.

## Compliance & legal (India)

- **Not a SEBI-registered investment adviser.** A `<Disclaimer/>` component on every analytics
  surface, in the footer, and on checkout. No buy/sell recommendations, no target prices, no
  "advice" language anywhere in copy.
- Backtest pages carry the "past performance" disclaimer.
- Terms & Conditions, Privacy Policy, Refund Policy pages before taking a single payment.
- **"Forever" plan** must state, at the point of sale, that it means the lifetime of the service.
  Treat prepaid lifetime revenue as deferred revenue in the books.
- GST-compliant invoices with GSTIN, HSN/SAC, place of supply.
- **Data licensing:** broker-sourced market data is licensed for the licensee's own use.
  Serve derived analytics; do not expose a raw-bar API to third parties without written
  clearance. Get a written data-redistribution opinion before enabling the public API tier.
- DPDP Act: consent record, data export and deletion endpoints, breach notification runbook.

## Reliability

- SLO: 99.5% monthly availability for the app; data published by 20:15 IST on ≥95% of trading days.
- Postgres: nightly `pg_dump` + continuous WAL archiving to R2; **restore drill monthly** — an
  untested backup is not a backup.
- Graceful degradation: if the pipeline fails, serve the last good `data_version` with a banner.
- Feature flags for anything touching money or data publishing.

## Accessibility

WCAG 2.2 AA. Keyboard-navigable table. Colour is never the sole carrier of meaning (pair
positive/negative colour with sign and, where dense, an arrow glyph).

## Cost envelope (indicative, monthly)

| Item | ₹ |
|---|---|
| Hetzner CCX33 (8 vCPU / 32 GB) | ~4,500 |
| Managed backups + R2 | ~800 |
| Kite Connect subscription | ~2,000 |
| Resend / Sentry / uptime | ~2,000 |
| **Total** | **~9,300** |

At ₹500/month, the infrastructure breaks even at ~19 subscribers. That is the number that
matters for the business case.
