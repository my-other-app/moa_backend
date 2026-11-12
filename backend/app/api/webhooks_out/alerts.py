"""Telling an endpoint's owner it was turned off after failing for three days.

- A club's own endpoint: a notice in the club's bell (`webhook_disabled`)
  and a mail to the club's owner.
- A partner's: the same notice in the club's bell (the club sees which
  partner stopped hearing from it), and a mail to the partner's contact
  (seams.partner_contact) -- or, until the partner API says who that is, an
  admin alert (`webhook_endpoint_disabled`).

The mail is a job of its own (`webhooks.notice_mail`) written in the same
transaction: retried when the provider is away, never sent for a change
rolled back.
"""
from __future__ import annotations

import logging
from urllib.parse import urlsplit

from app.config import settings
from app.core import jobs

logger = logging.getLogger(__name__)

MAIL_JOB = "webhooks.notice_mail"
SETTINGS_PATH = "/settings/integrations"


def _where(url: str) -> str:
    try:
        return urlsplit(url).hostname or url
    except ValueError:
        return url


async def endpoint_disabled(session, endpoint) -> None:
    from app.api.club_alerts import service as club_alerts
    from app.api.subscriptions.notices import owner_address
    from app.api.webhooks_out import seams

    host = _where(endpoint.url)
    since = endpoint.failing_since.date().isoformat() if endpoint.failing_since else ""
    partner = endpoint.partner_app_id is not None or endpoint.created_via == "partner_app"
    contact = await seams.partner_contact(session, endpoint.partner_app_id, endpoint.owner_client_id) if partner else None
    who = (contact[0] if contact else None) or ("a connected app" if partner else None)
    title = f"Webhooks to {host} were turned off"
    body = (
        f"Every delivery to {endpoint.url} has failed since {since}, so MyOtherApp stopped sending to it. "
        + (f"It belongs to {who}. " if who else "")
        + "Fix the endpoint, then turn it back on in Settings -> Integrations; failed deliveries can be sent again from its log."
    )
    await club_alerts.notify(
        session, endpoint.club_id, "webhook_disabled", title=title, body=body, link=SETTINGS_PATH,
        data={"endpoint_id": str(endpoint.id), "url": endpoint.url, "failing_since": since},
        dedupe_key=f"webhook_disabled:{endpoint.id}:{endpoint.disabled_at.isoformat() if endpoint.disabled_at else since}",
    )
    if partner:
        to = contact[1] if contact else None
        if not to:
            from app.api.admin_alerts import notify

            await notify(
                session, "webhook_endpoint_disabled", title=title, body=body, club=endpoint.club_id,
                data={"endpoint_id": str(endpoint.id), "partner_app_id": str(endpoint.partner_app_id or ""),
                      "client_id": endpoint.owner_client_id or "", "url": endpoint.url},
                dedupe_on={"endpoint_id": str(endpoint.id)},
            )
            return
        link = f"{settings.PUBLIC_SITE_URL.rstrip('/')}/developers"
    else:
        to = await owner_address(session, endpoint.club_id)
        link = f"{settings.PUBLIC_WEB_URL.rstrip('/')}{SETTINGS_PATH}"
    if to:
        await jobs.enqueue(session, MAIL_JOB, {"to": to, "subject": title, "paragraphs": [body], "button": ["Open MyOtherApp", link]})


@jobs.handler(MAIL_JOB, max_attempts=6, timeout=60.0, priority=jobs.HIGH)
async def _mail(payload: dict) -> None:
    import asyncio

    from app.api.subscriptions import notices

    button = payload.get("button")
    await asyncio.to_thread(
        notices.send, payload["to"], payload["subject"], list(payload.get("paragraphs") or []),
        tuple(button) if button else None, None,
    )
