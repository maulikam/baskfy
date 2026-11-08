"""``POST /support`` — the destination of the contact form (PROMPTS.md Prompt 18 §2).

docs/07 does not describe this endpoint. Prompt 18 asks for "``/support`` with a contact form",
and a form has to post somewhere; ``docs/DECISIONS.md`` §18.5 records the addition and why it is
here rather than in the web app.

**Why the API and not a Next server action.** The credential that sends mail is a secret, and
docs/11 §Security puts secrets "in the platform's secret store" behind the service that already
holds them. The web app has no mail transport, no rate limiter tied to the API's Redis, and no
principal — routing the form through the API reuses all three.

**What it does not do.** It stores nothing. docs/04 has no support-ticket table and inventing one
would be a migration in a content module; the message is delivered as email, which is where it
would be read anyway. That means a delivery failure loses the message, so — unlike the auth
endpoints, which swallow send failures to avoid becoming a membership oracle — this one surfaces
the failure to the sender as a 500. A visitor who is told "that did not send" will try again; one
who is told "thanks" will not.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, status

from baskfy_api.auth import PrincipalDep, settings_for
from baskfy_api.email import Mailer, build_transport
from baskfy_api.email import templates as mail
from baskfy_api.problems import Problem, ProblemType
from baskfy_api.schemas import AcceptedOut, SupportMessageIn

log = logging.getLogger(__name__)

router = APIRouter(tags=["support"])


def _mailer(request: Request) -> Mailer:
    """The process-wide mailer, built once by ``create_app`` — same accessor as the auth router."""
    mailer = getattr(request.app.state, "mailer", None)
    if isinstance(mailer, Mailer):
        return mailer
    return Mailer(build_transport(settings_for(request)))


@router.post(
    "/support",
    response_model=AcceptedOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Send a support message",
)
async def send_support_message(
    request: Request,
    body: SupportMessageIn,
    principal: PrincipalDep,
) -> AcceptedOut:
    """Deliver the form to the configured support address, and a copy to the sender.

    Unauthenticated on purpose: someone who cannot sign in is precisely the person who most needs
    to reach support. The global anonymous rate limit (docs/07 §Conventions, 10/min per address)
    is what keeps that from being a flood, and the message can only ever be addressed to *our*
    inbox — there is no recipient parameter, so this is not an open relay.
    """
    settings = settings_for(request)
    mailer = _mailer(request)

    delivered = await mailer.deliver(
        mail.support_request(
            settings.support_email,
            mail.SupportSubmission(
                topic=body.topic,
                from_name=body.name,
                from_email=body.email,
                body_text=body.message,
                account_public_id=principal.public_id,
            ),
        )
    )
    if not delivered:
        # `Mailer.deliver` has already logged the failure with the recipient; the topic is the
        # only thing added here, and no part of the message body is ever logged.
        log.warning("support message could not be delivered", extra={"support_topic": body.topic})
        # `INTERNAL_ERROR` (500), not `PIPELINE_DEGRADED` (503): the data pipeline is fine and
        # saying otherwise would put a false signal in front of whoever is on call. A mail
        # transport we cannot reach is our failure, which is what docs/07's catalogue calls a 500.
        raise Problem(
            ProblemType.INTERNAL_ERROR,
            "The message could not be sent. Please try again in a moment.",
        )

    # Best-effort: the message *did* reach support, and failing the request now would invite a
    # duplicate send of something already delivered.
    await mailer.deliver(mail.support_receipt(body.email, topic=body.topic))

    return AcceptedOut(detail="Thanks — your message has been sent. We reply to every one.")
