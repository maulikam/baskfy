# Runbook — a Razorpay webhook needs replaying

**When:** money was taken and nothing was granted. A user says "I paid and I still cannot export",
or a `payment` row exists with no `subscription`, or a `webhook_event` sits at `status = 'failed'`.
**Verified against:** NOT YET — and more sharply than the other runbooks. **No Razorpay webhook
delivery has ever reached this code.** The signature scheme, the event names and the entity shapes
are written from the documented formats; the whole suite is network-blocked and every test drives
an `httpx.MockTransport` (`docs/DECISIONS.md` §13.17, CLAUDE.md §"Open items"). Confirm each step
against a test-mode delivery **before** taking real money.

## First: is it actually the webhook?

Three states look identical to the user and need different things.

```sql
-- 1. Did we ever see a delivery for this? Check by user, then by event.
SELECT event_id, event_type, status, note, received_at, processed_at
FROM webhook_event
ORDER BY received_at DESC
LIMIT 20;

-- 2. Is there a payment?
SELECT p.id, p.status, p.amount_inr, p.created_at, u.email
FROM payment p JOIN app_user u ON u.id = p.user_id
WHERE u.email = 'them@example.com'
ORDER BY p.created_at DESC;

-- 3. Is there a subscription, and what state is it in?
SELECT s.id, s.status, s.started_at, s.current_period_end, s.razorpay_subscription_id, pl.code
FROM subscription s JOIN plan pl ON pl.id = s.plan_id
JOIN app_user u ON u.id = s.user_id
WHERE u.email = 'them@example.com'
ORDER BY s.started_at DESC;
```

| What you find | What it is | What to do |
|---|---|---|
| No `webhook_event` at all | The delivery never arrived, or the signature failed | §1, §2 |
| `webhook_event.status = 'failed'` with a `note` | We received it and could not process it | §3 |
| `webhook_event.status = 'ignored'`, note "Already processed" | A replay. Working as designed | nothing |
| `webhook_event.status = 'processed'` but `subscription.status = 'past_due'` | The charge event has not arrived yet | wait, then §3 |
| Everything looks right but the user still cannot export | Entitlement resolution, not billing | §5 |

## 1. Did Razorpay try?

Razorpay Dashboard → Settings → Webhooks → the endpoint → **Deliveries**. It shows every attempt,
the response code we returned, and a **Resend** button.

Razorpay retries a non-2xx delivery on its own schedule. `POST /webhooks/razorpay` answers **200**
for a replay and 200 for an event it deliberately ignores, precisely so a retry does not hammer an
endpoint that has already done its job. The **only** non-2xx it returns is `400` for a bad
signature (`decile_api.routers.billing.razorpay_webhook`).

So: if the dashboard shows 400s, go to §2. If it shows 5xx, go to §3. If it shows nothing at all,
the webhook URL is wrong or was never configured — fix it in the dashboard and press **Resend**.

## 2. 400s — the signature does not verify

`DECILE_RAZORPAY_WEBHOOK_SECRET` is **not** the API key. Razorpay signs a webhook with the secret
configured *on that webhook*, in the dashboard. The two being confused is the most likely cause of
a 400 here.

```bash
# What the API thinks it has (length only — never print the secret).
docker compose -f infra/docker/compose.prod.yml exec api \
  python -c "import os;s=os.environ.get('DECILE_RAZORPAY_WEBHOOK_SECRET','');print('len',len(s))"
```

Verification is `hmac_sha256(secret, raw_body)`, compared against the `X-Razorpay-Signature`
header, over the **exact bytes** received — `decile_api.razorpay.verify_webhook_signature`. Anything
between Razorpay and the app that re-serialises the body (a proxy that pretty-prints JSON, a WAF
that normalises whitespace) breaks the MAC while leaving the JSON semantically identical. If the
secret is right and it still fails, that is the next thing to look at.

Once fixed, press **Resend** in the dashboard.

## 3. Replaying a delivery

### Preferred: Razorpay's own Resend

It sends the original body with a valid signature and the original `X-Razorpay-Event-Id`. Because
`event_id` is UNIQUE and the handler inserts `ON CONFLICT DO NOTHING`
(`decile_core.models.billing.WebhookEvent`), **a resend of an event we already processed is a
no-op.** Resending is safe. Resend first, ask questions after.

### If we already have the body but processing failed

`webhook_event.payload` holds exactly what Razorpay sent. Re-processing it needs the dedupe row
cleared first — that row is the idempotency, so while it exists nothing will run:

```sql
-- Look at why it failed before you clear it.
SELECT event_id, event_type, status, note FROM webhook_event WHERE event_id = 'evt_...';

-- Then delete the claim. Keep the payload: re-insert it below, or copy it out first.
--   THIS REMOVES THE IDEMPOTENCY GUARD FOR THIS EVENT. Only do it for an event whose
--   processing you know did not complete.
DELETE FROM webhook_event WHERE event_id = 'evt_...';
```

Then replay it into the handler directly, without a signature (this runs *inside* the app, past the
verification the HTTP route does):

```bash
uv run python - <<'EOF'
import asyncio, json
from decile_worker.db import run_in_session
from decile_api.billing import handle_event

PAYLOAD = json.loads(open("/tmp/event.json").read())   # the webhook_event.payload you saved
EVENT_ID = PAYLOAD["id"] if "id" in PAYLOAD else "evt_REPLACE_ME"
EVENT_TYPE = PAYLOAD["event"]

async def go(session):
    return await handle_event(
        session, event_type=EVENT_TYPE, event_id=EVENT_ID, payload=PAYLOAD
    )

print(run_in_session(go))
EOF
```

`handle_event` is documented as "safe to call five times with the same `event_id`" — the second
call returns `status="ignored", duplicate=True`.

### Do NOT hand-write a subscription row

It is tempting and it is wrong. `subscription.razorpay_subscription_id` is what every later event
(`charged`, `halted`, `cancelled`) is matched against
(`decile_api.billing._subscription_by_gateway_id`). A row invented without it is a subscription
that never renews, never expires and cannot be cancelled, and the divergence is only discovered
months later. If you truly must unblock a customer today, use an **entitlement override** (§5) —
it is visible, attributed, expiring, and it does not corrupt the billing history.

## 4. Refunds

**`payment.status` never becomes `refunded`.** `refund.*` events are acknowledged and ignored.
`docs/07` has no refund endpoint and `docs/11` names a Refund Policy page that is not written
(CLAUDE.md §"Open items"). A refund issued in the Razorpay dashboard therefore leaves our
`payment` row reading `captured`, and the GST invoice raised against it stands.

That is a real gap, not an oversight to work around at 3am. Refund in the dashboard, note it, and
raise it as work.

## 5. Unblocking a customer without touching billing

`/admin` → find the account → **Entitlements** → grant the feature, with a reason and an expiry.

```bash
curl -X PUT -H "Authorization: Bearer $STAFF_JWT" -H "Content-Type: application/json" \
  https://<host>/api/v1/admin/users/<public_id>/entitlements \
  -d '{"feature":"export_csv","effect":"grant","reason":"webhook evt_xxx not delivered; paid 2026-08-21","expires_at":"2026-09-21T00:00:00Z"}'
```

This writes an `entitlement_override` row and an `admin_action` row naming you, and
`decile_api.entitlements.entitlements_for` layers it over the plan — so the *same* resolution every
gated endpoint uses now grants it (`docs/DECISIONS.md` §17.2). **Set an expiry.** A support grant
with no end is how a comp account is created by accident.

Then go back and fix the subscription properly. The override is a bandage.

## Before the first real charge

Every one of the following is unconfirmed, and the first three can each take money and grant
nothing:

* the signature scheme against a live test-mode delivery;
* the event names (`subscription.charged`, `subscription.halted`, …) and their entity shapes;
* the `X-Razorpay-Event-Id` header's presence — when it is absent the handler falls back to keying
  idempotency on the signature, which works but is untested against a real delivery;
* the GST rate (18%) and SAC code (998439), neither confirmed by a chartered accountant
  (`docs/DECISIONS.md` §13.2);
* that the advertised prices are GST-**inclusive** (`docs/DECISIONS.md` §13.1).
